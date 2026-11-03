"""Connection ownership regressions without requiring a running hypervisor."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

asyncssh = pytest.importorskip("asyncssh")
pytest.importorskip("libvirt")

from src.runtime.qemu_runtime import QemuRuntime


@pytest.fixture
def runtime(tmp_path: Path) -> QemuRuntime:
    runtime = object.__new__(QemuRuntime)
    runtime._ssh_states = {}
    runtime._ssh_key_path = tmp_path / "key"
    runtime._ssh_user = "root"
    runtime._ssh_timeout = 1
    runtime._disk_dir = tmp_path
    runtime._host_key_cache = {}
    runtime._get_workspace_vm_ip = MagicMock(return_value="127.0.0.1")
    runtime._ensure_memory_reclaim = MagicMock()
    return runtime


def connection() -> MagicMock:
    conn = MagicMock()
    conn.is_closed.return_value = False
    conn.wait_closed = AsyncMock()
    return conn


async def test_open_connection_is_reused_without_exec_probe(runtime) -> None:
    conn = connection()
    runtime._ssh_state("vm").connection = conn
    with patch("src.runtime.qemu_runtime.asyncssh.connect") as connect:
        results = await asyncio.gather(*(runtime._get_ssh("vm") for _ in range(20)))
    assert all(result is conn for result in results)
    conn.run.assert_not_called()
    conn.close.assert_not_called()
    connect.assert_not_called()


async def test_parallel_reconnects_publish_one_connection_with_keepalives(
    runtime,
) -> None:
    stale = connection()
    stale.is_closed.return_value = True
    runtime._ssh_state("vm").connection = stale
    conn = connection()
    runtime._load_tofu_key = MagicMock(return_value="pinned-key")
    runtime._save_tofu_key = AsyncMock()
    with patch(
        "src.runtime.qemu_runtime.asyncssh.connect", new=AsyncMock(return_value=conn)
    ) as connect:
        results = await asyncio.gather(*(runtime._get_ssh("vm") for _ in range(20)))
    assert all(result is conn for result in results)
    connect.assert_awaited_once()
    assert connect.await_args.kwargs["known_hosts"] == "pinned-key"
    assert connect.await_args.kwargs["keepalive_interval"] == 30
    assert connect.await_args.kwargs["keepalive_count_max"] == 5


async def test_readiness_and_reconnect_share_connection_and_verify_host_key(runtime):
    conn = connection()
    runtime._load_tofu_key = MagicMock(return_value="pinned-key")
    runtime._save_tofu_key = AsyncMock()
    with patch(
        "src.runtime.qemu_runtime.asyncssh.connect", new=AsyncMock(return_value=conn)
    ) as connect:
        assert await runtime._wait_for_ssh("vm") == "127.0.0.1"
        assert await runtime._get_ssh("vm") is conn
    connect.assert_awaited_once()
    assert connect.await_args.kwargs["known_hosts"] == "pinned-key"
    assert connect.await_args.kwargs["keepalive_interval"] == 30
    assert connect.await_args.kwargs["keepalive_count_max"] == 5


@pytest.mark.parametrize("readiness", [False, True])
async def test_host_key_mismatch_is_not_retried_or_trusted(runtime, readiness):
    runtime._load_tofu_key = MagicMock(return_value="pinned-key")
    runtime._save_tofu_key = AsyncMock()
    with (
        patch(
            "src.runtime.qemu_runtime.asyncssh.connect",
            new=AsyncMock(
                side_effect=asyncssh.HostKeyNotVerifiable("changed host key")
            ),
        ) as connect,
        pytest.raises(RuntimeError, match="SSH host key mismatch"),
    ):
        if readiness:
            await runtime._wait_for_ssh("vm")
        else:
            await runtime._get_ssh("vm")
    connect.assert_awaited_once()
    runtime._save_tofu_key.assert_not_awaited()
    assert runtime._ssh_state("vm").connection is None


@pytest.mark.parametrize("readiness", [False, True])
async def test_invalidation_rejects_late_connection_and_queued_acquire(
    runtime, readiness
):
    connecting = asyncio.Event()
    finish = asyncio.Event()
    conn = connection()

    async def delayed_connect(*args, **kwargs):
        connecting.set()
        await finish.wait()
        return conn

    with patch("src.runtime.qemu_runtime.asyncssh.connect", new=delayed_connect):
        acquire = asyncio.create_task(
            runtime._wait_for_ssh("vm") if readiness else runtime._get_ssh("vm")
        )
        await connecting.wait()
        queued = asyncio.create_task(runtime._get_ssh("vm"))
        await asyncio.sleep(0)
        state = runtime._ssh_state("vm")
        await runtime._invalidate_ssh("vm")
        finish.set()
        for task in (acquire, queued):
            with pytest.raises(RuntimeError, match="SSH lifecycle changed"):
                await task
    assert runtime._ssh_state("vm") is state
    assert state.connection is None
    conn.close.assert_called_once()
    with pytest.raises(RuntimeError, match="SSH lifecycle changed"):
        await runtime._get_ssh("vm")


async def test_readiness_reenables_ssh_after_deliberate_stop(runtime):
    old = connection()
    state = runtime._ssh_state("vm")
    state.connection = old
    await runtime._invalidate_ssh("vm")
    assert state.blocked
    old.close.assert_called_once()
    conn = connection()
    runtime._save_tofu_key = AsyncMock()
    with patch(
        "src.runtime.qemu_runtime.asyncssh.connect", new=AsyncMock(return_value=conn)
    ):
        await runtime._wait_for_ssh("vm")
        assert await runtime._get_ssh("vm") is conn
    assert not state.blocked


async def test_cancellation_before_publication_closes_candidate(runtime):
    saving = asyncio.Event()
    conn = connection()

    async def save(*args):
        saving.set()
        await asyncio.Event().wait()

    runtime._save_tofu_key = save
    with patch(
        "src.runtime.qemu_runtime.asyncssh.connect", new=AsyncMock(return_value=conn)
    ):
        task = asyncio.create_task(runtime._get_ssh("vm"))
        await saving.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    conn.close.assert_called_once()
    assert runtime._ssh_state("vm").connection is None


async def test_invalidation_cleanup_is_bounded_and_does_not_remove_state(runtime):
    conn = connection()
    conn.wait_closed.side_effect = asyncio.TimeoutError()
    state = runtime._ssh_state("vm")
    state.connection = conn
    await runtime._invalidate_ssh("vm")
    conn.abort.assert_called_once()
    assert runtime._ssh_state("vm") is state
    assert state.connection is None and state.blocked


async def test_authentication_failure_is_not_cached_or_retried(runtime) -> None:
    with (
        patch(
            "src.runtime.qemu_runtime.asyncssh.connect",
            new=AsyncMock(side_effect=asyncssh.PermissionDenied("denied")),
        ) as connect,
        pytest.raises(RuntimeError, match="SSH reconnect"),
    ):
        await runtime._get_ssh("vm")
    connect.assert_awaited_once()
    assert runtime._ssh_state("vm").connection is None


async def test_public_start_replaces_silent_transport_from_stopped_guest(runtime):
    import libvirt

    old = connection()
    runtime._ssh_state("vm").connection = old
    domain = MagicMock()
    domain.state.return_value = (libvirt.VIR_DOMAIN_SHUTOFF, 0)
    runtime._get_domain = MagicMock(return_value=domain)
    runtime._save_tofu_key = AsyncMock()
    new = connection()
    with patch(
        "src.runtime.qemu_runtime.asyncssh.connect", new=AsyncMock(return_value=new)
    ) as connect:
        await runtime.start_workspace("vm")
    old.close.assert_called_once()
    domain.create.assert_called_once()
    connect.assert_awaited_once()
    assert runtime._ssh_state("vm").connection is new


@pytest.mark.parametrize("operation", ["stop", "remove"])
async def test_public_lifecycle_fences_late_and_queued_connections(
    runtime, operation
):
    import libvirt

    connecting = asyncio.Event()
    finish = asyncio.Event()
    candidate = connection()

    async def delayed_connect(*args, **kwargs):
        connecting.set()
        await finish.wait()
        return candidate

    domain = MagicMock()
    domain.state.return_value = (libvirt.VIR_DOMAIN_SHUTOFF, 0)
    runtime._get_domain = MagicMock(return_value=domain)
    runtime._libvirt_conn = MagicMock()
    runtime._libvirt_conn.return_value.lookupByName.return_value = domain
    runtime._assert_workspace_resources_unconsumed = AsyncMock()
    runtime._destroy_workspace_network = AsyncMock()
    with patch("src.runtime.qemu_runtime.asyncssh.connect", new=delayed_connect):
        acquire = asyncio.create_task(runtime._get_ssh("vm"))
        await connecting.wait()
        queued = asyncio.create_task(runtime._get_ssh("vm"))
        await asyncio.sleep(0)
        await getattr(runtime, f"{operation}_workspace")("vm")
        finish.set()
        for task in (acquire, queued):
            with pytest.raises(RuntimeError, match="SSH lifecycle changed"):
                await task
    candidate.close.assert_called_once()
    assert runtime._ssh_state("vm").connection is None
