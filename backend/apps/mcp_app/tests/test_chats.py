"""Chat tools: listing, transcripts, answering, sending and starting."""

from __future__ import annotations

import uuid

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from apps.accounts.models import User
from apps.harness.harness_service import HarnessService
from apps.harness.models import (
    HarnessMessage,
    HarnessPart,
    HarnessSession,
    QuestionRequest,
)
from apps.organizations.models import Membership, MembershipRole
from apps.runners.enums import WorkspaceStatus

from .conftest import fails, ok

pytestmark = pytest.mark.django_db(transaction=True)


def _chat(world, *, title="Fix login bug", status="idle", turns=1):
    """Persist a chat with *turns* user/assistant exchanges and tool steps."""
    session = HarnessSession.objects.create(
        workspace=world.workspace,
        organization_id=world.org.id,
        title=title,
        status=status,
        created_by=world.user,
        last_message_at=timezone.now(),
    )
    position = 0
    for turn in range(turns):
        HarnessMessage.objects.create(
            session=session,
            role="user",
            content=f"question {turn}",
            position=position,
        )
        reply = HarnessMessage.objects.create(
            session=session,
            role="assistant",
            finish="stop",
            position=position + 1,
            completed_at=timezone.now(),
        )
        HarnessPart.objects.create(
            message=reply,
            type="tool",
            state="completed",
            title="Run tests",
            output="42 passed",
            display={"tool": "bash", "summary": "Run tests"},
            position=0,
        )
        HarnessPart.objects.create(
            message=reply,
            type="text",
            state="completed",
            output=f"answer {turn}",
            position=1,
        )
        position += 2
    return session


async def test_chat_list_filters_by_workspace_and_state(world):
    idle = await sync_to_async(_chat)(world, title="Idle chat")
    await sync_to_async(_chat)(world, title="Busy chat", status="busy")

    everything = await ok(world.ctx(), "chat_list")
    running = await ok(
        world.ctx(), "chat_list", workspace="billing-api", state="running"
    )

    assert {c["title"] for c in everything["chats"]} == {"Idle chat", "Busy chat"}
    assert [c["title"] for c in running["chats"]] == ["Busy chat"]
    assert running["chats"][0]["workspace"] == "billing-api"
    found = await ok(world.ctx(), "chat_list", query="idle")
    assert found["chats"][0]["chat_id"] == str(idle.id)


async def test_chat_read_renders_turns_steps_and_paging(world):
    session = await sync_to_async(_chat)(world, turns=3)
    ctx = world.ctx()

    page = await ok(ctx, "chat_read", chat_id=str(session.id), limit=2)

    assert page["state"] == "idle"
    assert page["chat"]["workspace"]["name"] == "billing-api"
    assert page["total_messages"] == 6
    assert [m["index"] for m in page["messages"]] == [4, 5]
    assert page["messages"][1] == {
        "index": 5,
        "role": "assistant",
        "text": "answer 2",
        "activity": ["bash: Run tests"],
        "created_at": page["messages"][1]["created_at"],
    }
    assert page["next_before"] == 4

    older = await ok(ctx, "chat_read", chat_id=str(session.id), limit=2, before=4)
    assert [m["text"] for m in older["messages"]] == ["question 1", "answer 1"]

    full = await ok(ctx, "chat_read", chat_id=str(session.id), limit=1, detail="full")
    step = full["messages"][0]["activity"][0]
    detail = await ok(
        ctx, "chat_read", chat_id=str(session.id), part_id=step["part_id"]
    )
    assert detail["part"]["output"] == "42 passed"
    assert detail["part"]["tool"] == "bash"


async def test_pending_question_is_answered_with_chat_respond(world):
    session = await sync_to_async(_chat)(world, status="busy")
    request = await sync_to_async(QuestionRequest.objects.create)(
        organization_id=world.org.id,
        workspace_id=world.workspace.id,
        session_id=session.id,
        questions=[
            {
                "header": "DB",
                "question": "Which database?",
                "options": [{"label": "Postgres"}, {"label": "SQLite"}],
            }
        ],
        status="pending",
    )
    ctx = world.ctx()

    page = await ok(ctx, "chat_read", chat_id=str(session.id))
    listed = await ok(ctx, "chat_list", state="waiting_for_input")

    assert page["state"] == "waiting_for_input"
    pending = page["waiting_for_input"][0]
    assert pending["request_id"] == str(request.id)
    assert pending["questions"][0]["options"] == ["Postgres", "SQLite"]
    assert listed["chats"][0]["chat_id"] == str(session.id)

    wrong_kind = await fails(
        ctx,
        "chat_respond",
        chat_id=str(session.id),
        request_id=str(request.id),
        permission="once",
    )
    assert "pass answers" in wrong_kind

    answered = await ok(
        ctx,
        "chat_respond",
        chat_id=str(session.id),
        request_id=str(request.id),
        answers=["Postgres"],
    )
    assert answered["resolved"] == {"type": "question", "status": "answered"}
    await sync_to_async(request.refresh_from_db)()
    assert request.answers == ["Postgres"]

    stale = await fails(
        ctx,
        "chat_respond",
        chat_id=str(session.id),
        request_id=str(request.id),
        answers=["Postgres"],
    )
    assert "No pending request" in stale


async def test_chat_send_steers_a_running_agent(world, monkeypatch):
    session = await sync_to_async(_chat)(world, status="busy")
    steered = []

    async def steer_run(self, target, prompt, *, user_id=None):
        steered.append((target.id, prompt, user_id))

    monkeypatch.setattr(HarnessService, "is_running", lambda self, sid: True)
    monkeypatch.setattr(HarnessService, "steer_run", steer_run)
    ctx = world.ctx()

    result = await ok(ctx, "chat_send", chat_id=str(session.id), prompt="Use Postgres")
    blocked = await fails(
        ctx, "chat_send", chat_id=str(session.id), prompt="x", model="other"
    )

    assert result["delivery"] == "delivered_into_running_turn"
    assert steered == [(session.id, "Use Postgres", world.user.id)]
    assert "chat_stop" in blocked


async def test_chat_send_starts_a_turn_and_returns_the_reply(world, monkeypatch):
    session = await sync_to_async(_chat)(world)
    started = []

    async def start_run(self, target, prompt, **kwargs):
        started.append((target.mode, prompt, kwargs["user_id"]))

        def reply():
            HarnessMessage.objects.create(
                session=target, role="user", content=prompt, position=2
            )
            message = HarnessMessage.objects.create(
                session=target, role="assistant", finish="stop", position=3
            )
            HarnessPart.objects.create(
                message=message, type="text", output="Done.", position=0
            )

        await sync_to_async(reply)()

    monkeypatch.setattr(HarnessService, "start_run", start_run)

    result = await ok(
        world.ctx(),
        "chat_send",
        chat_id=str(session.id),
        prompt="Ship it",
        mode="plan",
        wait_seconds=5,
    )

    assert started == [("plan", "Ship it", world.user.id)]
    assert result["delivery"] == "new_turn"
    assert result["state"] == "idle"
    assert [m["text"] for m in result["reply"]] == ["Done."]


async def test_chats_need_a_running_workspace(world):
    world.workspace.status = WorkspaceStatus.STOPPED
    await sync_to_async(world.workspace.save)()

    error = await fails(world.ctx(), "chat_start", workspace="billing-api", prompt="Hi")

    assert "workspace_start" in error
    assert await sync_to_async(HarnessSession.objects.count)() == 0


async def test_chat_start_creates_a_chat_with_skills(world, monkeypatch):
    runs = []

    async def start_run(self, target, prompt, **kwargs):
        runs.append((prompt, kwargs["skill_ids"]))

    monkeypatch.setattr(HarnessService, "start_run", start_run)
    ctx = world.ctx()
    skill = await ok(ctx, "skill_create", name="Conventions", body="Be terse.")

    result = await ok(
        ctx,
        "chat_start",
        workspace="billing-api",
        prompt="Add a health check",
        title="Health check",
        skills=["Conventions"],
    )

    assert result["chat"]["title"] == "Health check"
    assert result["chat"]["skills"] == ["Conventions"]
    assert result["chat"]["engine"] == "native"
    assert runs == [("Add a health check", [skill["skill_id"]])]


async def test_chats_of_other_users_are_not_found(world):
    session = await sync_to_async(_chat)(world)

    def make_stranger():
        stranger = User.objects.create_user(email=f"s-{uuid.uuid4().hex[:8]}@x.com")
        Membership.objects.create(
            user=stranger, organization=world.org, role=MembershipRole.MEMBER
        )
        return stranger

    stranger = await sync_to_async(make_stranger)()
    ctx = world.ctx(user=stranger)

    assert "not found" in await fails(ctx, "chat_read", chat_id=str(session.id))
    assert "not found" in await fails(ctx, "chat_delete", chat_id=str(session.id))
    assert "is not a chat ID" in await fails(ctx, "chat_read", chat_id="Fix login bug")


async def test_chat_stop_is_a_noop_for_idle_chats(world):
    session = await sync_to_async(_chat)(world)

    result = await ok(world.ctx(), "chat_stop", chat_id=str(session.id))

    assert result["stopped"] is False
    assert result["note"] == "The agent was not running."


def test_tool_steps_are_named_once_and_repeats_fold():
    from apps.mcp_app.tools.chats import _collapse_repeats, _tool_label

    assert _tool_label("bash", "Run tests") == "bash: Run tests"
    assert (
        _tool_label(
            "mcp_playwright_playwright_browser_navigate",
            "Playwright / Playwright / browser_navigate",
        )
        == "Playwright / browser_navigate"
    )
    assert _tool_label("read", "") == "read"
    assert _collapse_repeats(["a", "a", "b", "a"]) == ["a (×2)", "b", "a"]
