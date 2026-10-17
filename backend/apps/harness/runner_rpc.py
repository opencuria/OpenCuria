"""Fenced Redis transport between durable harness workers and Socket.IO gateways.

Commands and reply data use ephemeral Redis pub/sub. Redis keys retain only
bounded request ownership metadata and one-shot acknowledgements. There is no
retry after publish: a disconnected caller cannot know whether the runner acted.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import asdict, dataclass
from typing import Any

import redis.asyncio as redis
import structlog
from asgiref.sync import sync_to_async
from django.utils import timezone

from .runtime_authority import RunAuthority, current_run_authority

log = structlog.get_logger(__name__)
REQUEST_CHANNEL = "opencuria:harness:runner-rpc:requests:v1"
FRONTEND_CHANNEL = "opencuria:harness:runner-rpc:frontend:v1"
PREFIX = "opencuria:harness:runner-rpc:v1"
ROUTE_TTL = 3600
DELIVERY_ACK_TIMEOUT = 12
REQUEST_ACK_TIMEOUT = 15

# Event names are a finite protocol, never arbitrary runner-side dispatch.
_RPC: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "harness:exec_stream": (
        frozenset({"harness:exec_stream", "harness:cancel"}),
        frozenset({"harness:exec_chunk", "harness:exec_done"}),
    ),
    "harness:exec_wait": (
        frozenset({"harness:exec_wait", "harness:cancel"}),
        frozenset({"harness:exec_wait_result"}),
    ),
    "harness:read_file": (
        frozenset({"harness:read_file", "harness:cancel"}),
        frozenset({"harness:read_file_chunk", "harness:read_file_result"}),
    ),
    "harness:write_file": (
        frozenset({"harness:write_file", "harness:cancel"}),
        frozenset({"harness:write_file_result"}),
    ),
    "harness:write_file_start": (
        frozenset(
            {
                "harness:write_file_start",
                "harness:write_file_chunk",
                "harness:write_file_finish",
                "harness:cancel",
            }
        ),
        frozenset({"harness:write_file_result"}),
    ),
    "harness:list": (
        frozenset({"harness:list", "harness:cancel"}),
        frozenset({"harness:list_result"}),
    ),
    "harness:stat": (
        frozenset({"harness:stat", "harness:cancel"}),
        frozenset({"harness:stat_result"}),
    ),
    "harness:desktop_action": (
        frozenset({"harness:desktop_action", "harness:cancel"}),
        frozenset({"harness:desktop_action_result"}),
    ),
    "harness:process_start": (
        frozenset({"harness:process_start"}),
        frozenset({"harness:process_start_result"}),
    ),
    "harness:process_list": (
        frozenset({"harness:process_list"}),
        frozenset({"harness:process_list_result"}),
    ),
    "harness:process_get": (
        frozenset({"harness:process_get"}),
        frozenset({"harness:process_get_result"}),
    ),
    "harness:process_stop": (
        frozenset({"harness:process_stop"}),
        frozenset({"harness:process_stop_result"}),
    ),
    "harness:process_verify": (
        frozenset({"harness:process_verify"}),
        frozenset({"harness:process_verify_result"}),
    ),
    "workspace:artifact_ensure": (
        frozenset({"workspace:artifact_ensure", "harness:cancel"}),
        frozenset({"workspace:artifact_ensure_result"}),
    ),
    "workspace:stream_start": (
        frozenset(
            {
                "workspace:stream_start",
                "workspace:stream_input",
                "workspace:stream_close",
            }
        ),
        frozenset(
            {
                "workspace:stream_output",
                "workspace:stream_closed",
                "workspace:stream_close_result",
            }
        ),
    ),
}
_RPC["git:operation"] = (
    frozenset({"git:operation"}),
    frozenset({"git:operation_result"}),
)
_REPLY_EVENTS = frozenset(event for _, replies in _RPC.values() for event in replies)
_CLOSING_MUTATIONS = frozenset(
    {"harness:cancel", "harness:process_stop", "workspace:stream_close"}
)


@dataclass(frozen=True)
class _Route:
    """Secret-free routing metadata for one run-owned runner operation."""

    worker_id: str
    run_id: str
    owner_token: str
    deployment_version: str
    root_run_id: str
    workspace_id: str
    runner_id: str
    kind: str
    route_id: str
    request_events: tuple[str, ...]
    reply_events: tuple[str, ...]

    def encode(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def decode(cls, raw: str) -> _Route:
        data = json.loads(raw)
        data["request_events"] = tuple(data["request_events"])
        data["reply_events"] = tuple(data["reply_events"])
        return cls(**data)

    def owns(self, authority: RunAuthority) -> bool:
        return (
            self.worker_id == authority.worker_id
            and self.run_id == str(authority.run_id)
            and self.owner_token == str(authority.owner_token)
            and self.deployment_version == authority.deployment_version
            and self.root_run_id == str(authority.root_run_id)
        )


def _safe_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and value[0].isalnum()
        and all(c.isalnum() or c in "._:-" for c in value)
    )


def _reference(event: str, payload: dict[str, Any]) -> tuple[str, str] | None:
    kind, field = (
        ("stream", "connection_id")
        if event.startswith("workspace:stream_")
        else ("request", "request_id")
    )
    value = payload.get(field)
    return (kind, value) if _safe_id(value) else None


def _route_key(worker_id: str, kind: str, route_id: str) -> str:
    return f"{PREFIX}:route:{worker_id}:{kind}:{route_id}"


def _route_index(kind: str, route_id: str) -> str:
    return f"{PREFIX}:route-index:{kind}:{route_id}"


def _global_route_key(kind: str, route_id: str) -> str:
    """Reserve one correlation ID across every worker process."""
    return f"{PREFIX}:route-global:{kind}:{route_id}"


def _reply_channel(worker_id: str) -> str:
    return f"{PREFIX}:worker:{worker_id}:replies"


def _ack_key(worker_id: str, message_id: str) -> str:
    return f"{PREFIX}:ack:{worker_id}:{message_id}"


def _json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("runner RPC payload must be an object")
    return json.loads(json.dumps(value, separators=(",", ":")))


def _authority_data(authority: RunAuthority) -> dict[str, str]:
    return {
        "run_id": str(authority.run_id),
        "owner_token": str(authority.owner_token),
        "worker_id": authority.worker_id,
        "deployment_version": authority.deployment_version,
        "root_run_id": str(authority.root_run_id),
    }


def _parse_authority(data: Any) -> RunAuthority:
    if not isinstance(data, dict):
        raise ValueError("missing run authority")
    return RunAuthority(
        run_id=uuid.UUID(str(data["run_id"])),
        owner_token=uuid.UUID(str(data["owner_token"])),
        worker_id=str(data["worker_id"]),
        deployment_version=str(data["deployment_version"]),
        root_run_id=uuid.UUID(str(data["root_run_id"])),
    )


def _route_authority(route: _Route) -> RunAuthority:
    return RunAuthority(
        run_id=uuid.UUID(route.run_id),
        owner_token=uuid.UUID(route.owner_token),
        worker_id=route.worker_id,
        deployment_version=route.deployment_version,
        root_run_id=uuid.UUID(route.root_run_id),
    )


def _is_terminal(event: str, payload: dict[str, Any]) -> bool:
    if event == "workspace:stream_output":
        return False
    if event == "workspace:stream_close_result":
        return bool(payload.get("closed")) and not bool(payload.get("eof"))
    if event == "workspace:stream_closed":
        return True
    return event in {
        "git:operation_result",
        "harness:exec_done",
        "harness:exec_wait_result",
        "harness:read_file_result",
        "harness:write_file_result",
        "harness:list_result",
        "harness:stat_result",
        "harness:desktop_action_result",
        "workspace:artifact_ensure_result",
        "harness:process_start_result",
        "harness:process_list_result",
        "harness:process_get_result",
        "harness:process_stop_result",
        "harness:process_verify_result",
    }


async def _worker_reply(event: str, payload: dict[str, Any], runner_id: str) -> bool:
    """Route a checked reply into existing local RunnerService registries."""
    from asgiref.sync import sync_to_async

    from apps.runners.sio_server import get_runner_service

    service = get_runner_service()
    if event == "git:operation_result":
        result = await sync_to_async(service.handle_git_reply)(
            event, payload, runner_id=runner_id
        )
        return True  # Git resolver validates the pending workspace/operation.
    if event.startswith("harness:process_"):
        result = await sync_to_async(service.handle_process_reply)(
            event, payload, runner_id=runner_id
        )
    elif event.startswith("workspace:stream_"):
        result = await sync_to_async(service.handle_stream_reply)(
            event, payload, runner_id=runner_id
        )
    else:
        result = await sync_to_async(service.handle_harness_reply)(
            event, payload, runner_id=runner_id
        )
    return bool(result)


class HarnessRunnerRpcClient:
    """Worker-side reply routing and request publisher."""

    def __init__(
        self,
        worker_id: str,
        redis_url: str | None = None,
        *,
        reply_handler=None,
        control_plane: bool = False,
    ) -> None:
        self.worker_id = worker_id
        self.control_plane = control_plane
        self.redis_url = redis_url or os.environ.get("REDIS_URL", "")
        self.reply_handler = reply_handler or _worker_reply
        self._redis: redis.Redis | None = None
        self._pubsub: Any = None
        self._listener: asyncio.Task[None] | None = None
        self._pending: dict[
            tuple[str, str],
            tuple[RunAuthority, str, str, frozenset[str], frozenset[str]],
        ] = {}
        self._lock = asyncio.Lock()
        self._renewal: asyncio.Task | None = None

    async def start(self) -> None:
        """Subscribe before publish so fast runner replies cannot be lost."""
        if self._listener and not self._listener.done():
            return
        async with self._lock:
            if self._listener and not self._listener.done():
                return
            if not self.redis_url:
                raise RuntimeError("REDIS_URL is required for harness worker RPC")
            self._redis = redis.from_url(self.redis_url, decode_responses=True)
            self._pubsub = self._redis.pubsub()
            await self._pubsub.subscribe(_reply_channel(self.worker_id))
            while True:
                message = await self._pubsub.get_message(timeout=1.0)
                if message and message.get("type") == "subscribe":
                    break
            self._listener = asyncio.create_task(
                self._listen(), name=f"harness-rpc-{self.worker_id}"
            )
            self._renewal = asyncio.create_task(self._renew_routes())

    async def _renew_routes(self) -> None:
        """Keep quiet long-running streams routable across hours-long gates."""
        while True:
            await asyncio.sleep(30)
            if self._redis is None:
                return
            for kind, route_id in tuple(self._pending):
                keys = (
                    _route_key(self.worker_id, kind, route_id),
                    _route_index(kind, route_id),
                    _global_route_key(kind, route_id),
                )
                for key in keys:
                    await self._redis.expire(key, ROUTE_TTL)

    async def _listen(self) -> None:
        """Resolve replies only after local route and durable authority match."""
        assert self._pubsub is not None and self._redis is not None
        try:
            async for message in self._pubsub.listen():
                if message.get("type") != "message":
                    continue
                delivery_id = ""
                accepted = False
                try:
                    envelope = json.loads(message["data"])
                    delivery_id = str(envelope.get("delivery_id", ""))
                    accepted = await self._handle_reply(envelope)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning(
                        "harness_rpc_reply_failed",
                        worker_id=self.worker_id,
                        error=type(exc).__name__,
                    )
                if _safe_id(delivery_id):
                    key = _ack_key(self.worker_id, delivery_id)
                    await self._redis.rpush(key, json.dumps({"accepted": accepted}))
                    await self._redis.expire(key, 30)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(
                "harness_rpc_listener_failed",
                worker_id=self.worker_id,
                error=type(exc).__name__,
            )

    async def _handle_reply(self, envelope: dict[str, Any]) -> bool:
        event, payload, runner_id = (
            envelope.get("event"),
            envelope.get("payload"),
            envelope.get("runner_id"),
        )
        if (
            not isinstance(event, str)
            or event not in _REPLY_EVENTS
            or not isinstance(payload, dict)
            or not isinstance(runner_id, str)
        ):
            return False
        ref = _reference(event, payload)
        if ref is None:
            return False
        pending = self._pending.get(ref)
        if pending is None:
            return False
        authority, workspace_id, expected_runner, requests, replies = pending
        if envelope.get("authority") != _authority_data(authority):
            return False
        if (
            event not in replies
            or payload.get("workspace_id") != workspace_id
            or runner_id != expected_runner
        ):
            return False
        accepted = await self.reply_handler(event, payload, runner_id)
        if accepted and _is_terminal(event, payload):
            self._pending.pop(ref, None)
        return accepted

    async def emit(
        self,
        runner_id: str,
        event: str,
        payload: dict[str, Any],
        *,
        call: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Publish one finite-protocol request and await gateway acceptance."""
        await self.start()
        authority = current_run_authority()
        if authority is None and self.control_plane:
            authority = RunAuthority(
                uuid.UUID(int=0),
                uuid.UUID(int=0),
                self.worker_id,
                "control-plane",
                uuid.UUID(int=0),
            )
        if authority is None or authority.worker_id != self.worker_id:
            raise RuntimeError("Runner RPC has no current worker run authority")
        clean = _json_object(payload)
        ref = _reference(event, clean)
        if ref is None:
            raise RuntimeError(f"Unsupported runner RPC event: {event}")
        spec = next((v for v in _RPC.values() if event in v[0]), None)
        current = self._pending.get(ref)
        if event == "harness:cancel" and current is not None:
            spec = (current[3], current[4])
        if spec is None:
            raise RuntimeError(f"Runner RPC event is not permitted: {event}")
        workspace_id = clean.get("workspace_id")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError("workspace_id is required for runner RPC")
        if current is None:
            if len(self._pending) >= 8192:
                raise RuntimeError(
                    "Runner RPC pending limit reached; refusing new work"
                )
            self._pending[ref] = (authority, workspace_id, runner_id, spec[0], spec[1])
        elif (
            current[0] != authority
            or current[1] != workspace_id
            or current[2] != runner_id
            or event not in current[3]
        ):
            raise RuntimeError("Runner RPC request identity collision")
        message_id = uuid.uuid4().hex
        assert self._redis is not None
        envelope = {
            "protocol": 1,
            "message_id": message_id,
            "worker_id": self.worker_id,
            "authority": _authority_data(authority),
            "runner_id": runner_id,
            "event": event,
            "payload": clean,
            "control_plane": self.control_plane,
            "call": bool(call),
            "timeout": max(1, min(int(timeout or 15), 600)),
        }
        await self._redis.publish(
            REQUEST_CHANNEL, json.dumps(envelope, separators=(",", ":"))
        )
        try:
            response = await self._redis.blpop(
                _ack_key(self.worker_id, message_id),
                timeout=max(1, int(timeout or REQUEST_ACK_TIMEOUT)),
            )
        except Exception:
            from .runtime_authority import mark_current_run_uncertain

            mark_current_run_uncertain()
            raise
        if response is None:
            # The gateway may have issued a side effect; never resend it.
            from .runtime_authority import mark_current_run_uncertain

            mark_current_run_uncertain()
            raise TimeoutError("Harness runner RPC acknowledgement timed out")
        result = json.loads(response[1])
        if not result.get("accepted"):
            # Rejection can follow a failed ACK after dispatch. Treat it as
            # unknown, not as an ordinary tool failure inviting model retry.
            from .runtime_authority import mark_current_run_uncertain

            mark_current_run_uncertain()
            raise RuntimeError("Harness gateway rejected runner RPC")
        return result.get("result") if isinstance(result.get("result"), dict) else {}

    async def publish_frontend_event(
        self, event: str, payload: dict[str, Any], workspace_id: str
    ) -> None:
        """Publish run-owned UI updates to a shared gateway Socket.IO room."""
        await self.start()
        authority = current_run_authority()
        if authority is None or authority.worker_id != self.worker_id:
            raise RuntimeError("Frontend event has no current worker run authority")
        message = {
            "protocol": 1,
            "authority": _authority_data(authority),
            "event": str(event),
            "workspace_id": str(workspace_id),
            "payload": _json_object(payload),
        }
        assert self._redis is not None
        encoded = json.dumps(message, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 190 * 1024 * 1024:
            raise ValueError("frontend event exceeds transport maximum")
        await self._redis.publish(FRONTEND_CHANNEL, encoded)

    async def close(self) -> None:
        """Stop listener and release Redis connections on graceful worker exit."""
        if self._renewal is not None:
            self._renewal.cancel()
            await asyncio.gather(self._renewal, return_exceptions=True)
            self._renewal = None
        task, self._listener = self._listener, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._pubsub is not None:
            await self._pubsub.aclose()
            self._pubsub = None
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


class WorkerRpcSocket:
    """RunnerService transport retaining its local pending/accessor registries."""

    def __init__(self, client: HarnessRunnerRpcClient, fallback=None) -> None:
        self.client = client
        self.fallback = fallback

    async def _runner_id(self, sid: str) -> str:
        from apps.runners.models import Runner

        runner_id = await sync_to_async(
            lambda: (
                Runner.objects.filter(sid=sid, status="online")
                .values_list("id", flat=True)
                .first()
            )
        )()
        if runner_id is None:
            raise RuntimeError("Runner socket identity is no longer current")
        return str(runner_id)

    async def emit(self, event: str, data: dict, *, to: str, **kwargs) -> None:
        """Publish once; an unknown acknowledgement is never replayed."""
        if self.fallback is not None and not any(
            event in req for req, _ in _RPC.values()
        ):
            return await self.fallback.emit(event, data, to=to, **kwargs)
        await self.client.emit(await self._runner_id(to), event, data)

    async def call(self, event: str, data: dict, *, to: str, timeout=15, **kwargs):
        """Forward ACKed stream control through the stable socket owner."""
        if self.fallback is not None and not event.startswith("workspace:stream_"):
            return await self.fallback.call(
                event, data, to=to, timeout=timeout, **kwargs
            )
        return await self.client.emit(
            await self._runner_id(to), event, data, call=True, timeout=timeout
        )

    async def emit_frontend(self, event: str, data: dict, workspace_id: str) -> None:
        await self.client.publish_frontend_event(event, data, workspace_id)


def authorize_runner_request(runner_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Revalidate the exact run token at the runner's execution boundary.

    Unscoped administrative operations are already admitted by backend services;
    workspace lifecycle locks remain authoritative. No active run is replaced.
    """
    from apps.runners.repositories import WorkspaceRepository

    try:
        workspace_id = uuid.UUID(str(data.get("workspace_id", "")))
        workspace = WorkspaceRepository.get_by_id(workspace_id)
        if workspace is None or str(workspace.runner_id) != runner_id:
            return {"ok": False}
        authority_data = data.get("run_authority")
        if authority_data is None:
            # Agent-originated managed requests always carry an authority. Never
            # substitute a busy session's owner for a missing run token.
            return {"ok": not bool(data.get("requires_run_authority"))}
        authority = _parse_authority(authority_data)
        _workspace, denied = _authorize_runner_rpc(
            authority,
            str(data.get("event", "")),
            {"workspace_id": str(workspace_id), "action": data.get("action", "")},
            runner_id,
        )
        return {"ok": not denied}
    except (ValueError, TypeError, KeyError):
        return {"ok": False}


class HarnessRunnerRpcGateway:
    """Per-gateway cross-release broker. Replies route through Redis to owners."""

    def __init__(self, redis_url: str | None = None) -> None:
        self.redis_url = redis_url or os.environ.get("REDIS_URL", "")
        self._redis: redis.Redis | None = None
        self._pubsub: Any = None
        self._task: asyncio.Task[None] | None = None
        self._sio: Any = None
        self._runner_service: Any = None
        self._role = "api"
        self._process_id = uuid.uuid4().hex
        self._lock = asyncio.Lock()
        self._dispatch_tasks: set[asyncio.Task] = set()

    def _spawn_dispatch(self, data: dict[str, Any]) -> None:
        """Process control concurrently with streamed replies, with a hard bound."""
        if len(self._dispatch_tasks) >= 256:
            log.warning("harness_rpc_gateway_overloaded")
            return
        task = asyncio.create_task(self._handle(data))
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._dispatch_tasks.discard)

    def configure_runner_service(self, service: Any, *, role: str = "worker") -> None:
        """Install this broker process's runner service and role."""
        if role not in {"api", "worker"}:
            raise ValueError("Gateway broker role must be api|worker")
        self._runner_service = service
        self._role = role

    async def start(self, sio: Any) -> None:
        """Subscribe to worker requests when gateway ASGI starts."""
        if self._task and not self._task.done():
            return
        async with self._lock:
            if self._task and not self._task.done():
                return
            if not self.redis_url:
                raise RuntimeError("REDIS_URL is required for gateway runner RPC")
            self._sio = sio
            self._redis = redis.from_url(self.redis_url, decode_responses=True)
            self._pubsub = self._redis.pubsub()
            await self._pubsub.subscribe(REQUEST_CHANNEL, FRONTEND_CHANNEL)
            while True:
                message = await self._pubsub.get_message(timeout=1.0)
                if message and message.get("type") == "subscribe":
                    break
            self._task = asyncio.create_task(self._listen(), name="harness-rpc-gateway")

    async def _listen(self) -> None:
        """Deduplicate requests across blue/green gateways before dispatch."""
        assert self._pubsub is not None
        try:
            async for message in self._pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    if message.get("channel") == FRONTEND_CHANNEL:
                        await self._handle_frontend(json.loads(message["data"]))
                    else:
                        self._spawn_dispatch(json.loads(message["data"]))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("harness_rpc_request_failed", error=type(exc).__name__)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("harness_rpc_gateway_listener_failed", error=type(exc).__name__)

    async def _handle_frontend(self, data: dict[str, Any]) -> None:
        """Authorize a worker UI event against the owning durable run."""
        if self._redis is None or self._sio is None or self._role != "worker":
            return
        try:
            if data.get("protocol") != 1:
                return
            authority = _parse_authority(data.get("authority"))
            event = data.get("event")
            workspace_id = str(data.get("workspace_id", ""))
            payload = _json_object(data.get("payload"))
            valid = await sync_to_async(
                _authorize_frontend_event, thread_sensitive=True
            )(authority, workspace_id)
            if (
                not valid
                or not isinstance(event, str)
                or not event.startswith("harness.")
            ):
                return
            await self._sio.emit(
                event,
                payload,
                to=f"workspace:{workspace_id}",
                namespace="/frontend",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "harness_rpc_frontend_relay_failed",
                error=type(exc).__name__,
            )

    async def _handle(self, data: dict[str, Any]) -> None:
        """Authorize before one runner dispatch and ACK only gateway acceptance."""
        if self._redis is None or self._sio is None or self._role != "worker":
            return
        worker_id, message_id = data.get("worker_id"), data.get("message_id")
        if (
            data.get("protocol") != 1
            or not _safe_id(worker_id)
            or not _safe_id(message_id)
        ):
            return
        ack = _ack_key(worker_id, message_id)
        if not await self._redis.set(
            f"{PREFIX}:claim:{message_id}", "1", nx=True, ex=60
        ):
            return
        try:
            event = data.get("event")
            payload = _json_object(data.get("payload"))
            authority = _parse_authority(data.get("authority"))
            runner_id = data.get("runner_id")
            if (
                authority.worker_id != worker_id
                or not _safe_id(runner_id)
                or not isinstance(event, str)
            ):
                raise ValueError("bad RPC identity")
            route = await self._bind_route(authority, runner_id, event, payload)
            if route is None:
                await self._ack(ack, {"accepted": False})
                return
            if data.get("control_plane") is True:
                if authority.run_id.int or not worker_id.startswith("api-"):
                    raise ValueError("Invalid control-plane identity")
                workspace, denied = await sync_to_async(_authorize_api_rpc)(
                    event, payload, runner_id
                )
            else:
                workspace, denied = await sync_to_async(
                    _authorize_runner_rpc, thread_sensitive=True
                )(authority, event, payload, runner_id)
            if denied or workspace is None:
                await self._ack(ack, {"accepted": False})
                return
            if not workspace.runner.is_online or not workspace.runner.sid:
                await self._ack(ack, {"accepted": False})
                return
            if authority.run_id.int:
                payload = {**payload, "run_authority": _authority_data(authority)}
            service = self._runner_service
            if service is None:
                await self._ack(ack, {"accepted": False})
                return
            is_call = bool(data.get("call"))
            if is_call and not event.startswith("workspace:stream_"):
                await self._ack(ack, {"accepted": False})
                return
            if is_call:
                # Stream-control ACKs are runner-local; the broker's current
                # gateway owns the actual socket and call registry.
                result = await service._call_runner(
                    workspace.runner,
                    event,
                    payload,
                    timeout=max(1, min(int(data.get("timeout", 15)), 600)),
                )
                await self._ack(
                    ack,
                    {
                        "accepted": True,
                        "result": result if isinstance(result, dict) else {},
                    },
                )
            else:
                await service._emit_to_runner(workspace.runner, event, payload)
                await self._ack(ack, {"accepted": True})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "harness_rpc_dispatch_failed",
                worker_id=worker_id,
                error=type(exc).__name__,
            )
            await self._ack(ack, {"accepted": False})

    async def _bind_route(
        self,
        authority: RunAuthority,
        runner_id: str,
        event: str,
        payload: dict[str, Any],
    ) -> _Route | None:
        """Set an expiring, immutable route before allowing a runner reply."""
        if self._redis is None:
            return None
        ref = _reference(event, payload)
        spec = next((v for v in _RPC.values() if event in v[0]), None)
        if ref is None:
            return None
        kind, route_id = ref
        workspace_id = payload.get("workspace_id")
        if not isinstance(workspace_id, str) or not workspace_id:
            return None
        key = _route_key(authority.worker_id, kind, route_id)
        if event == "harness:cancel":
            raw = await self._redis.get(key)
            if raw is None:
                return None
            try:
                current = _Route.decode(raw)
            except (ValueError, TypeError, KeyError):
                return None
            return (
                current
                if (
                    current.owns(authority)
                    and current.workspace_id == workspace_id
                    and current.runner_id == runner_id
                    and event in current.request_events
                )
                else None
            )
        if spec is None:
            return None
        global_key = _global_route_key(kind, route_id)
        reserved = await self._redis.set(
            global_key, authority.worker_id, nx=True, ex=ROUTE_TTL
        )
        if not reserved and await self._redis.get(global_key) != authority.worker_id:
            return None
        route = _Route(
            worker_id=authority.worker_id,
            run_id=str(authority.run_id),
            owner_token=str(authority.owner_token),
            deployment_version=authority.deployment_version,
            root_run_id=str(authority.root_run_id),
            workspace_id=workspace_id,
            runner_id=runner_id,
            kind=kind,
            route_id=route_id,
            request_events=tuple(sorted(spec[0])),
            reply_events=tuple(sorted(spec[1])),
        )
        created = await self._redis.set(key, route.encode(), nx=True, ex=ROUTE_TTL)
        if created:
            index = _route_index(kind, route_id)
            await self._redis.sadd(index, authority.worker_id)
            await self._redis.expire(index, ROUTE_TTL)
            return route
        raw = await self._redis.get(key)
        if raw is None:
            return None
        try:
            current = _Route.decode(raw)
        except (ValueError, TypeError, KeyError):
            return None
        if (
            not current.owns(authority)
            or current.workspace_id != workspace_id
            or current.runner_id != runner_id
            or event not in current.request_events
        ):
            return None
        await self._redis.expire(key, ROUTE_TTL)
        return current

    async def route_reply(
        self, event: str, payload: dict[str, Any], runner_id: str
    ) -> bool | None:
        """Deliver worker reply and return its actual local accessor acceptance."""
        if (
            self._redis is None
            or event not in _REPLY_EVENTS
            or not isinstance(payload, dict)
        ):
            return None
        ref = _reference(event, payload)
        if ref is None:
            return None
        kind, route_id = ref
        index = _route_index(kind, route_id)
        workers = await self._redis.smembers(index)
        if not workers:
            return None
        if len(workers) != 1:
            return False
        worker_id = next(iter(workers))
        key = _route_key(worker_id, kind, route_id)
        raw = await self._redis.get(key)
        if raw is None:
            await self._redis.srem(index, worker_id)
            return None
        try:
            route = _Route.decode(raw)
            authority = _route_authority(route)
        except (ValueError, TypeError, KeyError):
            return False
        if (
            event not in route.reply_events
            or payload.get("workspace_id") != route.workspace_id
            or runner_id != route.runner_id
        ):
            return False
        if not authority.run_id.int and authority.worker_id.startswith("api-"):
            workspace, denied = await sync_to_async(_authorize_api_rpc)(
                event, {"workspace_id": route.workspace_id}, runner_id
            )
        else:
            workspace, denied = await sync_to_async(
                _authorize_runner_rpc, thread_sensitive=True
            )(authority, event, {"workspace_id": route.workspace_id}, runner_id)
        if denied or workspace is None:
            return False
        delivery_id = uuid.uuid4().hex
        envelope = {
            "protocol": 1,
            "delivery_id": delivery_id,
            "authority": _authority_data(authority),
            "runner_id": runner_id,
            "event": event,
            "payload": payload,
        }
        encoded = json.dumps(envelope, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 190 * 1024 * 1024:
            return False
        await self._redis.publish(_reply_channel(worker_id), encoded)
        received = await self._redis.blpop(
            _ack_key(worker_id, delivery_id), timeout=DELIVERY_ACK_TIMEOUT
        )
        accepted = False
        if received is not None:
            try:
                accepted = bool(json.loads(received[1]).get("accepted"))
            except (ValueError, TypeError):
                accepted = False
        if accepted:
            if _is_terminal(event, payload):
                await self._redis.delete(key, _global_route_key(kind, route_id))
                await self._redis.srem(index, worker_id)
            else:
                await self._redis.expire(key, ROUTE_TTL)
                await self._redis.expire(index, ROUTE_TTL)
            return True
        return False

    async def _ack(self, key: str, body: dict[str, Any]) -> None:
        assert self._redis is not None
        await self._redis.rpush(key, json.dumps(body, separators=(",", ":")))
        await self._redis.expire(key, 30)

    async def close(self) -> None:
        """Stop listener and close Redis resources."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._dispatch_tasks:
            tasks = tuple(self._dispatch_tasks)
            for pending in tasks:
                pending.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._pubsub is not None:
            await self._pubsub.aclose()
            self._pubsub = None
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


def _authorize_api_rpc(event: str, payload: dict, runner_id: str):
    """Validate trusted control-plane dispatch after the API's user ACL checks."""
    from apps.runners.repositories import WorkspaceRepository

    if event not in _REPLY_EVENTS and not any(event in req for req, _ in _RPC.values()):
        return None, True
    try:
        workspace = WorkspaceRepository.get_by_id(
            uuid.UUID(str(payload.get("workspace_id", "")))
        )
    except (ValueError, TypeError):
        return None, True
    if workspace is None or str(workspace.runner_id) != runner_id:
        return None, True
    return workspace, False


def _authorize_frontend_event(authority: RunAuthority, workspace_id: str) -> bool:
    """Check durable ownership for UI streaming, including just-finished runs."""
    from apps.harness.models import HarnessRun

    try:
        workspace_uuid = uuid.UUID(workspace_id)
    except (ValueError, TypeError, AttributeError):
        return False
    run = HarnessRun.objects.filter(
        id=authority.run_id,
        owner_token=authority.owner_token,
        owner_worker_id=authority.worker_id,
        deployment_version=authority.deployment_version,
        session__workspace_id=workspace_uuid,
    ).first()
    if run is None:
        return False
    root_id = run.root_run_id or run.id
    if root_id != authority.root_run_id:
        return False
    if run.root_run_id:
        return HarnessRun.objects.filter(
            id=authority.root_run_id,
            owner_worker_id=authority.worker_id,
            deployment_version=authority.deployment_version,
        ).exists()
    return True


def _authorize_runner_rpc(
    authority: RunAuthority,
    event: str,
    payload: dict[str, Any],
    runner_id: str,
) -> tuple[Any | None, bool]:
    """Recheck durable run/worker/workspace ownership at the side-effect boundary."""
    from apps.harness.models import (
        HarnessRun,
        HarnessRunStatus,
        HarnessWorkerRegistration,
    )
    from apps.harness.repositories import HarnessSessionRepository
    from apps.runners.repositories import WorkspaceRepository

    try:
        workspace_id = uuid.UUID(str(payload.get("workspace_id", "")))
    except (ValueError, TypeError, AttributeError):
        return None, True
    run = (
        HarnessRun.objects.select_related("session")
        .filter(
            id=authority.run_id,
            owner_token=authority.owner_token,
            owner_worker_id=authority.worker_id,
            deployment_version=authority.deployment_version,
        )
        .first()
    )
    if run is None or run.session.workspace_id != workspace_id:
        return None, True
    root = (
        HarnessRun.objects.filter(
            id=run.root_run_id,
            owner_worker_id=authority.worker_id,
            deployment_version=authority.deployment_version,
        ).first()
        if run.root_run_id
        else run
    )
    if (
        root is None
        or root.id != authority.root_run_id
        or root.owner_worker_id != authority.worker_id
        or root.deployment_version != authority.deployment_version
    ):
        return None, True
    worker = HarnessWorkerRegistration.objects.filter(
        worker_id=authority.worker_id,
        deployment_version=authority.deployment_version,
        state__in=(
            HarnessWorkerRegistration.State.READY,
            HarnessWorkerRegistration.State.DRAINING,
        ),
        heartbeat_at__gte=timezone.now() - HarnessWorkerRegistration.HEARTBEAT_TIMEOUT,
    ).exists()
    if not worker or run.status not in (
        HarnessRunStatus.RUNNING,
        HarnessRunStatus.CLOSING,
    ):
        return None, True
    if (
        root.id != run.id
        and (
            root.status not in (HarnessRunStatus.RUNNING, HarnessRunStatus.CLOSING)
            or root.abort_requested_at is not None
        )
        and event not in _REPLY_EVENTS
        and event not in _CLOSING_MUTATIONS
    ):
        return None, True
    if run.status == HarnessRunStatus.RUNNING and run.abort_requested_at is not None:
        action = str(payload.get("action", ""))
        cleanup_desktop = event == "harness:desktop_action" and action in {
            "release",
            "lease_status",
        }
        if (
            event not in _REPLY_EVENTS
            and event not in _CLOSING_MUTATIONS
            and not cleanup_desktop
        ):
            return None, True
    if run.status == HarnessRunStatus.CLOSING:
        is_reply = event in _REPLY_EVENTS
        action = str(payload.get("action", ""))
        cleanup_desktop = event == "harness:desktop_action" and action in {
            "release",
            "lease_status",
        }
        if not is_reply and event not in _CLOSING_MUTATIONS and not cleanup_desktop:
            return None, True
    workspace = WorkspaceRepository.get_by_id(workspace_id)
    if workspace is None or str(workspace.runner_id) != runner_id:
        return None, True
    if not workspace.runner.is_online or not workspace.runner.sid:
        return None, True
    if run.status != HarnessRunStatus.CLOSING and event not in _REPLY_EVENTS:
        try:
            HarnessSessionRepository.ensure_interactions_available(workspace_id)
        except Exception:
            return None, True
    return workspace, False


_gateway: HarnessRunnerRpcGateway | None = None


def get_gateway_broker() -> HarnessRunnerRpcGateway:
    """Return lazy process singleton for the stable Socket.IO gateway."""
    global _gateway
    if _gateway is None:
        _gateway = HarnessRunnerRpcGateway()
    return _gateway
