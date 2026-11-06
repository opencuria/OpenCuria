"""API waiters fail fast without a live execution worker instead of hanging."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from apps.harness.command_models import (
    EXECUTION_HEARTBEAT_TTL_SECONDS,
    HarnessCommand,
    HarnessExecutionHeartbeat,
)
from apps.harness.command_repository import ExecutionHeartbeatRepository as Heartbeats
from apps.harness.command_repository import HarnessCommandRepository as Commands
from apps.harness.command_service import HarnessCommandClient
from apps.harness.tests.test_execution_commands import worker_for
from common.exceptions import ServiceUnavailableError


def _expire(owner_id: uuid.UUID) -> None:
    HarnessExecutionHeartbeat.objects.filter(owner_id=owner_id).update(
        beat_at=timezone.now() - timedelta(seconds=EXECUTION_HEARTBEAT_TTL_SECONDS + 1)
    )


def _client(**kwargs) -> HarnessCommandClient:
    return HarnessCommandClient(interval=0.001, liveness_interval=0.01, **kwargs)


async def _wait_for_command() -> HarnessCommand:
    async def queued():
        while not await sync_to_async(HarnessCommand.objects.exists)():
            await asyncio.sleep(0.001)
        return await sync_to_async(HarnessCommand.objects.get)()

    return await asyncio.wait_for(queued(), 3)


@pytest.mark.django_db(transaction=True)
def test_presence_is_fresh_per_owner_until_it_expires_or_is_removed():
    owner, other = uuid.uuid4(), uuid.uuid4()
    assert not Heartbeats.is_alive()
    Heartbeats.beat(owner)
    assert Heartbeats.is_alive()
    assert Heartbeats.is_alive(owner)
    assert not Heartbeats.is_alive(other)
    _expire(owner)
    assert not Heartbeats.is_alive()
    Heartbeats.beat(other)
    assert Heartbeats.prune() == 1
    assert list(
        HarnessExecutionHeartbeat.objects.values_list("owner_id", flat=True)
    ) == [other]
    Heartbeats.remove(other)
    assert not Heartbeats.is_alive()


@pytest.mark.django_db(transaction=True)
def test_only_unclaimed_requests_can_be_withdrawn():
    claimed = Commands.enqueue("abort_computeruse", {"workspace_id": "a"})
    owner = uuid.uuid4()
    assert Commands.claim(owner).id == claimed.id
    queued = Commands.enqueue("abort_computeruse", {"workspace_id": "b"})
    assert not Commands.withdraw(claimed.id)
    assert Commands.get(claimed.id).state == HarnessCommand.State.CLAIMED
    assert Commands.withdraw(queued.id)
    assert not HarnessCommand.objects.filter(pk=queued.id).exists()


@pytest.mark.django_db(transaction=True)
async def test_request_without_worker_is_withdrawn_and_reported_unavailable():
    with pytest.raises(ServiceUnavailableError) as raised:
        await asyncio.wait_for(
            _client().call("abort_computeruse", {"workspace_id": str(uuid.uuid4())}),
            3,
        )
    assert raised.value.code == "execution_unavailable"
    # A later worker must not perform an action the caller was told failed.
    assert not await sync_to_async(HarnessCommand.objects.exists)()


@pytest.mark.django_db(transaction=True)
async def test_queued_request_behind_live_worker_waits_as_backpressure():
    owner = uuid.uuid4()
    await sync_to_async(Heartbeats.beat)(owner)
    call = asyncio.create_task(
        _client().call("abort_computeruse", {"workspace_id": str(uuid.uuid4())})
    )
    try:
        row = await _wait_for_command()
        await asyncio.sleep(0.05)  # Several liveness checks at full capacity.
        assert not call.done()
        assert (await sync_to_async(Commands.get)(row.id)).state == "queued"
        await sync_to_async(Commands.claim)(owner)
        await sync_to_async(Commands.complete)(
            row.id, owner, {"ok": True, "value": ["aborted"]}
        )
        assert await asyncio.wait_for(call, 3) == ["aborted"]
    finally:
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)


@pytest.mark.django_db(transaction=True)
async def test_claimed_request_of_crashed_worker_fails_and_stays_for_recovery():
    owner = uuid.uuid4()
    await sync_to_async(Heartbeats.beat)(owner)
    call = asyncio.create_task(
        _client().call("abort_computeruse", {"workspace_id": str(uuid.uuid4())})
    )
    try:
        row = await _wait_for_command()
        await sync_to_async(Commands.claim)(owner)
        await sync_to_async(_expire)(owner)
        with pytest.raises(ServiceUnavailableError) as raised:
            await asyncio.wait_for(call, 3)
        assert raised.value.code == "execution_lost"
        # Effects may exist, so only startup recovery may settle this request.
        assert (await sync_to_async(Commands.get)(row.id)).state == "claimed"
    finally:
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)


@pytest.mark.django_db(transaction=True)
async def test_reply_published_just_before_owner_stopped_is_returned():
    owner = uuid.uuid4()
    reads = 0

    class Storage(Commands):
        @staticmethod
        def enqueue(*args):
            Commands.enqueue(*args)
            return Commands.claim(owner)

        @staticmethod
        def get(command_id):
            nonlocal reads
            reads += 1
            row = Commands.get(command_id)
            if reads == 1:
                # The owner replies and withdraws presence right after this read.
                Commands.complete(command_id, owner, {"ok": True, "value": ["done"]})
            return row

    client = HarnessCommandClient(
        repository=Storage, interval=0.001, liveness_interval=0
    )
    assert await asyncio.wait_for(
        client.call("abort_computeruse", {"workspace_id": str(uuid.uuid4())}), 3
    ) == ["done"]


@pytest.mark.django_db(transaction=True)
async def test_worker_renews_presence_until_its_commands_are_settled(tmp_path):
    crashed = uuid.uuid4()
    await sync_to_async(Heartbeats.beat)(crashed)
    await sync_to_async(_expire)(crashed)
    worker = worker_for(SimpleNamespace(), tmp_path)
    worker.heartbeat_interval = 0.01
    try:
        await worker.start()
        assert await sync_to_async(Heartbeats.is_alive)(worker.owner_id)
        assert not await sync_to_async(
            HarnessExecutionHeartbeat.objects.filter(owner_id=crashed).exists
        )()

        def beat_at():
            return HarnessExecutionHeartbeat.objects.get(
                owner_id=worker.owner_id
            ).beat_at

        first = await sync_to_async(beat_at)()

        async def renewed():
            while await sync_to_async(beat_at)() == first:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(renewed(), 3)
        await worker.stop()
        assert not await sync_to_async(Heartbeats.is_alive)()
    finally:
        await worker.stop()
        worker.guard.release_failed_start()
