"""Exercise real Codex admission without implementing its external runtime."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async

from apps.harness.models import HarnessMessage
from apps.harness.provider_turn_models import ProviderTurn
from apps.harness.tests.test_codex_domain import codex_setup, create_session
from apps.harness.tools.base import ToolContext, ToolError
from apps.harness.tools.subagents import TaskArgs
from common.exceptions import AuthenticationError

# Re-export the fixture for this test module.
__all__ = ["codex_setup"]


@pytest.mark.django_db(transaction=True)
async def test_each_answer_snapshots_current_provider_account(codex_setup):
    data = codex_setup
    service = data.service
    session = await sync_to_async(create_session)(data)
    entered, release = asyncio.Event(), asyncio.Event()
    observed = []

    async def execute(**kwargs):
        current = kwargs["session"]
        context = service._runs[str(current.id)]
        context["execution_started"] = True
        observed.append(dict(context["account_ids"]))
        entered.set()
        await release.wait()
        await context["ownership"].finalize_turn(
            status="completed", finish="stop", assistant_content="done"
        )
        await service._finalize_run(current, kwargs["assistant"])

    service._execute_run = execute
    with pytest.raises(AuthenticationError):
        await service.start_run(session, "foreign", user_id=data.owner.id + 1000)
    assert await sync_to_async(HarnessMessage.objects.count)() == 0
    first = await service.start_run(session, "answer one", user_id=data.owner.id)
    first_task = service._tasks[str(session.id)]
    await entered.wait()
    second_account = await sync_to_async(data.accounts.save_account)(
        organization_id=data.org,
        user=data.owner,
        provider="chatgpt",
        payload={
            "label": "Second",
            "access": "second-access",
            "refresh": "second-refresh",
            "account_id": "second-id",
        },
    )
    await sync_to_async(data.accounts.select_account)(
        data.org, data.owner.id, "chatgpt", second_account.id
    )
    turn = await sync_to_async(ProviderTurn.objects.get)(assistant_message_id=first.id)
    assert turn.account_ids["chatgpt"] == str(data.account.id)
    assert observed == [turn.account_ids]
    release.set()
    await first_task
    entered.clear()
    release.clear()
    second = await service.start_run(session, "answer two", user_id=data.owner.id)
    second_task = service._tasks[str(session.id)]
    await entered.wait()
    turn = await sync_to_async(ProviderTurn.objects.get)(assistant_message_id=second.id)
    assert turn.account_ids["chatgpt"] == str(second_account.id)
    assert observed[-1] == turn.account_ids
    release.set()
    await second_task
    await sync_to_async(first.refresh_from_db)()
    assert first.harness_id == "codex"
    assert first.provider == "chatgpt"
    assert first.content == "done"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "agent,override,message",
    [
        ("general", "openrouter/vendor/model", "provider cannot be changed"),
        ("computeruse", None, "computeruse is not supported"),
    ],
)
async def test_managed_task_rejects_provider_switch_or_computeruse(
    codex_setup, agent, override, message
):
    data = codex_setup
    parent = await sync_to_async(create_session)(data)
    ctx = ToolContext(
        session_id=str(parent.id),
        workspace_id=str(parent.workspace_id),
        accessor=SimpleNamespace(),
        model=parent.model,
        depth=0,
        max_depth=2,
    )
    with pytest.raises(ToolError, match=message):
        await data.service._run_subagent_tool(
            parent=parent,
            args=TaskArgs(
                description="Child",
                prompt="Investigate",
                subagent_type=agent,
                model_override=override,
            ),
            ctx=ctx,
            subtask_id="child",
            organization_id=data.org,
        )


@pytest.mark.django_db(transaction=True)
async def test_managed_task_happy_path_keeps_codex_owner_skills_and_snapshot(
    codex_setup,
    monkeypatch,
):
    import uuid

    from apps.harness.provider_turns import ProviderTurnService
    from apps.harness.repositories import HarnessMessageRepository

    data = codex_setup
    parent = await sync_to_async(create_session)(data)
    # Skill bodies are loaded at admission; this test isolates task routing.
    parent.skill_ids = [str(uuid.uuid4())]
    parent_assistant = await sync_to_async(HarnessMessageRepository.create)(
        session_id=parent.id, role="assistant", harness_id="codex"
    )
    snapshot = {"chatgpt": str(data.account.id)}
    turn = await sync_to_async(ProviderTurnService().admit)(
        assistant_message_id=parent_assistant.id,
        organization_id=data.org,
        user_id=data.owner.id,
        account_ids=snapshot,
    )
    data.service._runs[str(parent.id)] = {
        "message_id": str(parent_assistant.id),
        "tool_parts": {},
        "subtask_parts": {},
        "provider_turn_id": turn.id,
        "initiated_by_id": data.owner.id,
        "account_ids": snapshot,
    }
    # A changed selection must not affect the child being admitted now.
    await sync_to_async(data.accounts.select_account)(
        data.org, data.owner.id, "chatgpt", None
    )
    seen = []
    real_create = data.service.create_session

    def create(**kwargs):
        seen.append(kwargs)
        return real_create(**kwargs)

    data.service.create_session = create
    monkeypatch.setattr(
        "apps.harness.harness_service.resolve_skill_bodies",
        lambda *a, **kw: ["skill body"],
    )

    async def start(child, prompt, **kwargs):
        assert child.harness_id == "codex"
        assert kwargs["user_id"] == data.owner.id
        assert child.skill_ids == parent.skill_ids
        assert child.engine_state == {"provider_id": "chatgpt"}
        assert data.service._provider_turn_context(child)["account_ids"] == snapshot
        return await sync_to_async(HarnessMessageRepository.create)(
            session_id=child.id,
            role="assistant",
            content="Codex child done",
            harness_id="codex",
        )

    data.service.start_run = start
    ctx = ToolContext(
        session_id=str(parent.id),
        workspace_id=str(parent.workspace_id),
        accessor=SimpleNamespace(),
        model=parent.model,
        depth=0,
        max_depth=2,
    )
    result = await data.service._run_subagent_tool(
        parent=parent,
        args=TaskArgs(
            description="Child", prompt="Investigate", subagent_type="general"
        ),
        ctx=ctx,
        subtask_id="child",
        organization_id=data.org,
    )
    assert "Codex child done" in result.output
    assert seen[0]["user_id"] == data.owner.id
    assert seen[0]["skill_ids"] == parent.skill_ids
