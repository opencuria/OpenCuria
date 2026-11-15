"""Ephemeral runner replies shared by API and execution processes.

The API adapter authenticates the runner before calling the application reply router.
Each peer then applies the existing domain ownership and correlation checks in
its own process. Payloads (including file/stream chunks) stay on Redis pub/sub;
only run commands, never RPC data, belong in the durable execution queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

log = structlog.get_logger(__name__)

ReplyDispatcher = Callable[[str, dict, str], Awaitable[bool]]
ReplyOwnership = Callable[[str, dict], bool]
DisconnectHandler = Callable[[str], Awaitable[None]]


class RunnerReplyRelay:
    """A process peer with explicit routing dependencies and bounded ACK waits."""

    def __init__(
        self,
        redis_url: str,
        *,
        events: set[str],
        dispatch: ReplyDispatcher,
        owns: ReplyOwnership,
        disconnected: DisconnectHandler,
        channel: str = "opencuria:runner-replies",
        timeout: float = 5.0,
    ) -> None:
        self._redis = Redis.from_url(redis_url, decode_responses=True)
        self._events = frozenset(events)
        self._dispatch = dispatch
        self._owns = owns
        self._disconnected = disconnected
        self._channel = channel
        self._peer_id = uuid.uuid4().hex
        self._acks = f"{channel}:{self._peer_id}"
        self._timeout = timeout
        self._pubsub = self._redis.pubsub()
        self._task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._closed = False

    async def start(self) -> None:
        """Subscribe before accepting commands, so the first reply cannot race."""
        if self._task is not None:
            return
        if self._closed:
            raise RuntimeError("A stopped runner reply relay cannot be restarted")
        await self._redis.ping()
        await self._pubsub.subscribe(self._channel, self._acks)
        for _ in range(2):
            message = await self._pubsub.get_message(
                ignore_subscribe_messages=False, timeout=self._timeout
            )
            if message is None or message.get("type") != "subscribe":
                raise RuntimeError("Runner reply subscription was not acknowledged")
        self._task = asyncio.create_task(self._listen(), name="runner-reply-relay")

    async def route(self, event: str, data: dict, runner_id: str) -> bool:
        """Deliver locally or to the peer owning the correlated request/stream."""
        if event not in self._events or not isinstance(data, dict) or not runner_id:
            return False
        if await self._dispatch(event, data, runner_id):
            return True
        return await self.forward(event, data, runner_id)

    async def forward(self, event: str, data: dict, runner_id: str) -> bool:
        """Forward an authenticated reply already declined by the local consumer."""
        if event not in self._events or not isinstance(data, dict) or not runner_id:
            return False
        if self._task is None or self._closed:
            return False
        correlation = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[correlation] = future
        try:
            listeners = await self._redis.publish(
                self._channel,
                json.dumps(
                    {
                        "kind": "reply",
                        "origin": self._peer_id,
                        "correlation": correlation,
                        "runner_id": runner_id,
                        "event": event,
                        "data": data,
                    }
                ),
            )
            if listeners <= 1:
                return False
            return await asyncio.wait_for(future, self._timeout)
        except (asyncio.TimeoutError, RedisError):
            log.warning("runner_reply_relay_unavailable", reply_event=event)
            return False
        finally:
            self._pending.pop(correlation, None)
            if not future.done():
                future.cancel()

    async def runner_disconnected(self, runner_id: str) -> None:
        """Broadcast transport loss; the publishing peer already failed locally."""
        if self._task is None or self._closed:
            return
        await self._redis.publish(
            self._channel,
            json.dumps(
                {
                    "kind": "disconnected",
                    "origin": self._peer_id,
                    "runner_id": runner_id,
                }
            ),
        )

    async def _listen(self) -> None:
        while not self._closed:
            try:
                async for message in self._pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    try:
                        envelope = json.loads(message["data"])
                        await self._receive(envelope)
                    except (TypeError, ValueError, KeyError):
                        log.warning("runner_reply_relay_invalid_envelope")
                    except Exception:
                        log.exception("runner_reply_relay_delivery_failed")
            except asyncio.CancelledError:
                raise
            except RedisError:
                # Redis reconnects and resubscribes this pubsub on the next read.
                # In-flight replies have bounded waits and are never replayed.
                log.warning("runner_reply_relay_reconnecting")
                for future in self._pending.values():
                    if not future.done():
                        future.set_result(False)
                await asyncio.sleep(0.5)

    async def _receive(self, envelope: Any) -> None:
        if not isinstance(envelope, dict):
            return
        kind = envelope.get("kind")
        if kind == "ack":
            future = self._pending.get(str(envelope.get("correlation", "")))
            if future is not None and not future.done():
                future.set_result(envelope.get("accepted") is True)
            return
        origin = envelope.get("origin")
        if not isinstance(origin, str) or origin == self._peer_id:
            return
        runner_id = envelope.get("runner_id")
        if not isinstance(runner_id, str) or not runner_id:
            return
        if kind == "disconnected":
            await self._disconnected(runner_id)
            return
        event = envelope.get("event")
        data = envelope.get("data")
        correlation = envelope.get("correlation")
        if (
            kind != "reply"
            or event not in self._events
            or not isinstance(data, dict)
            or not isinstance(correlation, str)
            or not self._owns(event, data)
        ):
            return
        accepted = await self._dispatch(event, data, runner_id)
        await self._redis.publish(
            f"{self._channel}:{origin}",
            json.dumps(
                {"kind": "ack", "correlation": correlation, "accepted": accepted}
            ),
        )

    async def stop(self) -> None:
        """Release subscriptions and wake all waiters during process shutdown."""
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for future in self._pending.values():
            if not future.done():
                future.set_result(False)
        await self._pubsub.aclose()
        await self._redis.aclose()
