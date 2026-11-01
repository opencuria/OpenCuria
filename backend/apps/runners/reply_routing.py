"""Application routing of authenticated runner replies across execution processes."""

from __future__ import annotations

import os

from asgiref.sync import sync_to_async

from common.runner_reply_relay import RunnerReplyRelay

from .services import RunnerService, get_runner_service
from .services.infra.harness_gateway import get_harness_gateway

RUNNER_REPLY_METHODS = {
    **dict.fromkeys(
        (
            "harness:exec_wait_result",
            "harness:read_file_result",
            "harness:read_file_chunk",
            "harness:write_file_result",
            "harness:list_result",
            "harness:desktop_action_result",
            "workspace:artifact_ensure_result",
        ),
        "handle_harness_reply",
    ),
    "git:operation_result": "handle_git_reply",
    **dict.fromkeys(
        (
            "harness:process_start_result",
            "harness:process_list_result",
            "harness:process_get_result",
            "harness:process_stop_result",
            "harness:process_verify_result",
        ),
        "handle_process_reply",
    ),
    "workspace:stream_output": "handle_stream_reply",
    "workspace:stream_closed": "handle_stream_reply",
}


def _owns_local_reply(event: str, data: dict) -> bool:
    """Select a live waiter without querying unrelated peers' persistence."""
    method = RUNNER_REPLY_METHODS.get(event)
    if method == "handle_git_reply":
        return get_runner_service().rpc_registry.owns_request(
            "git", str(data.get("request_id", ""))
        )
    if method == "handle_process_reply":
        return get_runner_service().rpc_registry.owns_request(
            "process", str(data.get("request_id", ""))
        )
    return get_harness_gateway().owns_runner_reply(event, data)


async def _dispatch_local(event: str, data: dict, runner_id: str) -> bool:
    """Keep domain checks identical in API and execution worker processes."""
    method = RUNNER_REPLY_METHODS.get(event)
    if method is None:
        return False
    return bool(
        await sync_to_async(getattr(get_runner_service(), method))(
            event, data, runner_id=runner_id
        )
    )


async def _fail_runner_streams(runner_id: str) -> None:
    """Wake up remote harness streams when their transport disconnects."""
    await sync_to_async(get_runner_service().fail_streams_for_runner)(runner_id)


_relay: RunnerReplyRelay | None = None


async def start_runner_reply_relay() -> None:
    """Start this API/execution process's reply transport infrastructure."""
    global _relay
    if _relay is not None:
        return
    relay = RunnerReplyRelay(
        os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        events=set(RUNNER_REPLY_METHODS),
        dispatch=_dispatch_local,
        owns=_owns_local_reply,
        disconnected=_fail_runner_streams,
    )
    try:
        await relay.start()
    except BaseException:
        await relay.stop()
        raise
    _relay = relay


async def stop_runner_reply_relay() -> None:
    """Stop this process's subscriptions after dispatch has drained."""
    global _relay
    relay, _relay = _relay, None
    if relay is not None:
        await relay.stop()


async def route_runner_reply(
    event: str,
    data: dict,
    runner_id: str,
    *,
    service: RunnerService,
) -> bool:
    """Run the local consumer, then forward to the remote correlation owner."""
    method = RUNNER_REPLY_METHODS.get(event)
    if method is None or not isinstance(data, dict) or not runner_id:
        return False
    accepted = await sync_to_async(getattr(service, method))(
        event, data, runner_id=runner_id
    )
    if accepted:
        return True
    if _relay is None:
        return False
    return await _relay.forward(event, data, runner_id)


async def notify_runner_disconnected(runner_id: str) -> None:
    """Publish a fenced, successful runner disconnect to all execution peers."""
    if _relay is not None:
        await _relay.runner_disconnected(runner_id)
