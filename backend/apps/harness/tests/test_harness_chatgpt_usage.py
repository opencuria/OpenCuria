"""ChatGPT Responses usage flows through the real harness to DB and frontend."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest

from apps.harness.harness_service import FRONTEND_EVENT_PART, HarnessService
from apps.harness.providers.chatgpt import ChatGPTAdapter
from apps.harness.providers.chatgpt_oauth import CODEX_API_ENDPOINT
from apps.harness.repositories import HarnessPartRepository, HarnessSessionRepository
from apps.harness.tests.conftest import FakeAccessor
from apps.runners.models import Workspace


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("event_type", ["response.done", "response.completed"])
async def test_chatgpt_terminal_usage_persisted_and_emitted(
    harness_workspace: Workspace, event_type: str
) -> None:
    """Terminal Responses events retain inclusive usage across harness layers."""
    events: list[dict[str, Any]] = []
    requests: list[httpx.Request] = []
    response_events = [
        {"type": "response.output_text.delta", "delta": "Hello"},
        {"type": "response.output_text.delta", "delta": " world"},
        {
            "type": event_type,
            "response": {
                "status": "completed",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                    "input_tokens_details": {"cached_tokens": 2},
                },
            },
        },
    ]
    payload = "".join(
        f"data: {json.dumps(event)}\n\n" for event in response_events
    ) + "data: [DONE]\n\n"

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == CODEX_API_ENDPOINT
        assert json.loads(request.content)["model"] == "gpt-6.1-sol"
        return httpx.Response(
            200, text=payload, headers={"Content-Type": "text/event-stream"}
        )

    async def emit(event: str, data: dict[str, Any]) -> None:
        events.append({"event": event, **data})

    async def accessor_factory(workspace_id: str) -> FakeAccessor:
        return FakeAccessor(workspace_id)

    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="ChatGPT usage",
        agent_name="build",
        mode="build",
        model="chatgpt/gpt-6.1-sol",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        adapter = ChatGPTAdapter(
            {
                "access": "test-access",
                "refresh": "test-refresh",
                "expires": int(time.time() * 1000) + 3_600_000,
                "account_id": "test-account",
            },
            client=client,
        )
        service = HarnessService(
            emit=emit,
            provider_factory=lambda _org: adapter,
            accessor_factory=accessor_factory,
        )
        assistant = await service.start_run(
            session,
            "Say hello",
            organization_id=harness_workspace.runner.organization_id,
            workspace_id=str(harness_workspace.id),
        )
        await asyncio.wait_for(service._tasks[str(session.id)], timeout=10)

    assert len(requests) == 1
    assistant.refresh_from_db()
    session.refresh_from_db()
    assert assistant.content == "Hello world"
    assert assistant.finish == "stop"
    assert session.status == "idle"
    # Cached input is already included: it must not inflate prompt or total.
    assert assistant.tokens == {"prompt": 10, "completion": 5, "total": 15}
    assert session.tokens == assistant.tokens
    tokens = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    parts = HarnessPartRepository.list_for_session(session.id)
    finished = [part for part in parts if part.type == "step-finish"]
    assert len(finished) == 1
    assert finished[0].state == "completed"
    assert finished[0].meta["tokens"] == tokens
    token_events = [
        event
        for event in events
        if event["event"] == FRONTEND_EVENT_PART
        and "tokens" in event.get("delta", {})
    ]
    assert len(token_events) == 1
    assert token_events[0]["message_id"] == str(assistant.id)
    assert token_events[0]["part_id"] == str(finished[0].id)
    assert token_events[0]["delta"]["tokens"] == tokens
    assert "step_finish" in token_events[0]["delta"]
