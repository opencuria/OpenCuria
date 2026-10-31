"""Tests for brief session recaps focused on the user's requests."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from asgiref.sync import sync_to_async

from apps.harness.agents.definitions import get_agent
from apps.harness.harness_service import HarnessService
from apps.harness.models import (
    HarnessMessage,
    HarnessMessageRole,
    HarnessPart,
    HarnessSession,
    HarnessSessionStatus,
)
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.permissions.service import PermissionService
from apps.harness.provider_turns import ProviderTurnService
from apps.harness.providers.base import Delta, ProviderAdapter, Usage
from apps.harness.providers.openrouter import OpenRouterAdapter
from apps.harness.repositories import (
    HarnessMessageRepository,
    HarnessPartRepository,
    HarnessSessionRepository,
)
from apps.harness.schemas import session_to_out
from apps.harness.services import ProviderConfigService
from apps.harness.session_metadata_service import (
    SessionMetadataService,
    _normalize_generated_summary,
)
from apps.harness.todos.domain import TodoItem
from apps.harness.todos.repositories import TodoRepository

SENTENCE = "Login-Fehler und zugehörige Tests korrigieren."


class RoleProvider(ProviderAdapter):
    """Answers the summary agent with a sentence, everything else with prose."""

    name = "fake"

    def __init__(self) -> None:
        self.summary_inputs: list[str] = []

    async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
        """Pick the answer by the agent's system prompt."""
        system = str(messages[0].content or "")
        if "Summarize what the user wanted" in system:
            self.summary_inputs.append(str(messages[-1].content or ""))
            yield Delta(text=SENTENCE, usage=Usage(1, 1, 2))
        elif "short title" in system:
            yield Delta(text="Login fix", usage=Usage(1, 1, 2))
        else:
            yield Delta(text="I fixed the login bug.", usage=Usage(1, 1, 2))


async def _drop_emit(event: str, data: dict) -> None:
    return None


def _service(provider: ProviderAdapter, emit=_drop_emit) -> HarnessService:
    return HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
        emit=emit,
        provider_factory=lambda _org: provider,
    )


def _metadata_service(
    provider: ProviderAdapter | None, emit=_drop_emit
) -> SessionMetadataService:
    """Exercise metadata without constructing session run orchestration."""

    async def emit_metadata(event: str, data: dict, workspace_id: str) -> None:
        await emit(event, data)

    async def conversations_changed(workspace_id: uuid.UUID) -> None:
        await emit("harness.conversations_changed", {"workspace_id": str(workspace_id)})

    return SessionMetadataService(
        emit=emit_metadata,
        conversations_changed=conversations_changed,
        provider_factory=(lambda _org: provider) if provider is not None else None,
    )


def _configure_small_model(workspace) -> None:
    ProviderConfigService().save_config(
        organization_id=workspace.runner.organization_id,
        small_model="small-model",
    )


def _session_with_answer(workspace, *, harness_id: str = "native", **message):
    """Idle root session with one finished user/assistant exchange."""
    session = HarnessSessionRepository.create(
        workspace_id=workspace.id,
        organization_id=workspace.runner.organization_id,
        title="Fix login",
        agent_name="build",
        mode="build",
        model="big-model",
        harness_id=harness_id,
    )
    HarnessMessageRepository.create(
        session_id=session.id, role=HarnessMessageRole.USER, content="Fix the login"
    )
    assistant = HarnessMessageRepository.create(
        session_id=session.id,
        role=HarnessMessageRole.ASSISTANT,
        content=message.get("content", "Fixed the login bug in auth.py."),
    )
    HarnessMessageRepository.complete(
        assistant,
        finish=message.get("finish", "stop"),
        error=message.get("error", ""),
    )
    return session, assistant


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Bug fixed, review next.  ", "Bug fixed, review next."),
        ('"Bug fixed, review next."', "Bug fixed, review next."),
        ("**Bug fixed**, `review` next.", "Bug fixed, review next."),
        ("Bug fixed.\n\nNext: review the diff.", "Bug fixed."),
        ("\n\n", ""),
        ("", ""),
    ],
)
def test_normalize_generated_summary(raw: str, expected: str) -> None:
    """Output is reduced to one plain line without wrapping quotes."""
    assert _normalize_generated_summary(raw) == expected


def test_normalize_generated_summary_clamps_at_a_word_boundary() -> None:
    """Overlong output is cut at a word boundary and marked with an ellipsis."""
    result = _normalize_generated_summary("word " * 60)
    assert result.endswith("word…")
    assert len(result) <= 101


@pytest.mark.django_db
def test_set_summary_for_turn_only_applies_to_latest_idle_turn(harness_workspace):
    """A slow generation must not overwrite a newer run or a busy session."""
    session, first = _session_with_answer(harness_workspace)
    second = HarnessMessageRepository.create(
        session_id=session.id, role=HarnessMessageRole.ASSISTANT, content="again"
    )

    assert not HarnessSessionRepository.set_summary_for_turn(
        session.id, first.id, "outdated"
    )
    HarnessSessionRepository.mark_status(session, HarnessSessionStatus.BUSY)
    assert not HarnessSessionRepository.set_summary_for_turn(
        session.id, second.id, "busy"
    )
    HarnessSessionRepository.mark_status(session, HarnessSessionStatus.IDLE)
    assert HarnessSessionRepository.set_summary_for_turn(
        session.id, second.id, "  current  "
    )
    assert HarnessSession.objects.get(id=session.id).summary == "current"


@pytest.mark.django_db
def test_clear_summary(harness_workspace):
    """clear_summary empties the sentence of one session."""
    session, assistant = _session_with_answer(harness_workspace)
    HarnessSessionRepository.set_summary_for_turn(session.id, assistant.id, "old")
    HarnessSessionRepository.clear_summary(session.id)
    assert HarnessSession.objects.get(id=session.id).summary == ""


def test_summary_agent_focuses_on_user_intent_without_tools() -> None:
    """The hidden agent describes the request, not execution status or next steps."""
    agent = get_agent("summary")
    assert "at most 10 words" in agent.system_prompt
    assert "user's requests and latest clarifications" in agent.system_prompt
    assert "not progress, results, or next steps" in agent.system_prompt
    assert "user's language" in agent.system_prompt
    assert agent.permissions == {"*": "deny", "question": "deny"}
    assert agent.model_override == "small"


@pytest.mark.django_db
def test_summary_material_carries_all_requests_and_closing_replies(
    harness_workspace, django_assert_num_queries
):
    """The whole conversation stays ordered, without Worked or status material."""
    session, first = _session_with_answer(
        harness_workspace, content="aggregate content must not leak"
    )
    HarnessPartRepository.create(
        message_id=first.id, type="text", output="I will inspect the login."
    )
    HarnessPartRepository.create(
        message_id=first.id, type="tool", output="private tool output"
    )
    HarnessPartRepository.create(
        message_id=first.id, type="text", output="Fixed the login bug."
    )
    HarnessMessageRepository.create(
        session_id=session.id, role="user", content="Also cover logout with tests."
    )
    second = HarnessMessageRepository.create(
        session_id=session.id, role="assistant", content="more aggregate content"
    )
    for part_type, text in [
        ("text", "Working on tests"),
        ("reasoning", "private reasoning"),
        ("steer", "Only regression tests, please."),
        ("text", "Tests cover both cases."),
        ("step-finish", ""),
    ]:
        HarnessPartRepository.create(
            message_id=second.id, type=part_type, output=text, state="completed"
        )
    HarnessMessageRepository.complete(second, finish="error", error="private error")
    TodoRepository().save(
        str(session.id), [TodoItem(content="unrelated next step", status="pending")]
    )
    # A slow recap may not include a newer turn or another session.
    HarnessMessageRepository.create(
        session_id=session.id, role="user", content="Future request"
    )
    _session_with_answer(harness_workspace, content="Other session answer")

    with django_assert_num_queries(2):
        material = _metadata_service(RoleProvider()).summary_material(
            session.id, second.id
        )

    assert material == (
        "User:\nFix the login\n\n"
        "Agent's final reply:\nFixed the login bug.\n\n"
        "User:\nAlso cover logout with tests.\n\n"
        "User:\nOnly regression tests, please.\n\n"
        "Agent's final reply:\nTests cover both cases."
    )


@pytest.mark.django_db
@pytest.mark.parametrize("turns", [1, 10, 50])
def test_summary_reads_only_required_payloads_in_two_queries(
    harness_workspace, django_assert_num_queries, turns: int
) -> None:
    """Growing histories never reload aggregate content or provider metadata."""
    session = HarnessSession.objects.create(
        workspace=harness_workspace,
        organization_id=harness_workspace.runner.organization_id,
    )
    messages = HarnessMessage.objects.bulk_create(
        [
            HarnessMessage(
                session=session,
                position=index,
                role="user" if index % 2 == 0 else "assistant",
                content="Request" if index % 2 == 0 else "unused" * 22000,
                engine_meta={"unused": "x" * 65536},
                tokens={"unused": "x" * 4096},
                error="unused" * 1000,
            )
            for index in range(turns * 2)
        ]
    )
    HarnessPart.objects.bulk_create(
        [
            HarnessPart(
                message=message,
                position=position,
                type=kind,
                output=output,
                input={"unused": "x" * 65536},
                meta={"unused": "x" * 65536},
            )
            for message in messages[1::2]
            for position, (kind, output) in enumerate(
                [("tool", "unused" * 22000), ("text", "Answer")]
            )
        ]
    )

    with django_assert_num_queries(2):
        rows = HarnessMessageRepository.list_for_summary(session.id, messages[-1].id)

    for row in rows:
        assert {
            "content",
            "engine_meta",
            "tokens",
            "error",
        } <= row.get_deferred_fields()
        assert row.summary_content == ("Request" if row.role == "user" else "")
        for part in row.summary_parts:
            assert {"output", "input", "meta"} <= part.get_deferred_fields()
            assert part.summary_text == ("Answer" if part.type == "text" else "")
    with django_assert_num_queries(2):
        material = _metadata_service(RoleProvider()).summary_material(
            session.id, messages[-1].id
        )
    assert material == "\n\n".join(
        ["User:\nRequest", "Agent's final reply:\nAnswer"] * turns
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        ([], ""),
        ([("text", "Final "), ("text", "answer")], "Final answer"),
        ([("text", "Working"), ("tool", "private result")], ""),
        ([("text", "Working"), ("tool", "private"), ("text", "")], ""),
        ([("reasoning", "private thoughts")], ""),
        ([("compaction", "private checkpoint")], ""),
        ([("tool", "private"), ("compaction", "private"), ("text", "Final")], "Final"),
        ([("text", "Old prose"), ("compaction", "private")], ""),
        ([("text", "Old prose"), ("steer", "Use pnpm")], ""),
        (
            [
                ("text", "Earlier response"),
                ("agent", "private plan"),
                ("tool", "private result"),
                ("text", "Intermediate note"),
                ("agent", "private plan"),
                ("reasoning", "private thoughts"),
                ("text", "Final "),
                ("text", "answer"),
                ("step-finish", ""),
            ],
            "Final answer",
        ),
    ],
)
def test_summary_material_only_uses_final_prose(harness_workspace, parts, expected):
    """Mirror the final-answer selection used by the chat's copy action."""
    session, assistant = _session_with_answer(
        harness_workspace, content="aggregate content must not leak"
    )
    for part_type, output in parts:
        HarnessPartRepository.create(
            message_id=assistant.id, type=part_type, output=output, state="completed"
        )
    material = _metadata_service(RoleProvider()).summary_material(
        session.id, assistant.id
    )
    user = "User:\nFix the login"
    if any(part_type == "steer" for part_type, _ in parts):
        user += "\n\nUser:\nUse pnpm"
    assert material == user + (
        f"\n\nAgent's final reply:\n{expected}" if expected else ""
    )


@pytest.mark.django_db
@pytest.mark.parametrize("hidden_tool", ["question", "task", "write"])
def test_summary_ignores_tools_hidden_by_the_timeline(harness_workspace, hidden_tool):
    """Invisible tool parts after the answer must not erase the closing prose."""
    session, assistant = _session_with_answer(harness_workspace)
    part_type = {"question": "tool", "task": "subtask", "write": "patch"}[hidden_tool]
    HarnessPartRepository.create(
        message_id=assistant.id, type=part_type, call_id="call", state="completed"
    )
    HarnessPartRepository.create(message_id=assistant.id, type="text", output="Final")
    HarnessPartRepository.create(
        message_id=assistant.id,
        type="tool",
        call_id="call",
        state="pending" if hidden_tool == "question" else "completed",
        input={"tool": hidden_tool},
    )
    assert _metadata_service(RoleProvider()).summary_material(
        session.id, assistant.id
    ) == ("User:\nFix the login\n\nAgent's final reply:\nFinal")


@pytest.mark.django_db
@pytest.mark.parametrize("finish", ["stop", "aborted", "error"])
def test_summary_can_describe_request_without_answer(harness_workspace, finish):
    """The user's goal remains known even when the agent has not replied."""
    session, assistant = _session_with_answer(
        harness_workspace, content="", finish=finish, error="private error"
    )
    assert _metadata_service(RoleProvider()).summary_material(
        session.id, assistant.id
    ) == ("User:\nFix the login")


@pytest.mark.django_db
def test_summary_material_does_not_clip_user_requests(harness_workspace):
    """Keep clarifications at the end of long prompts, not only their prefix."""
    session, assistant = _session_with_answer(harness_workspace)
    HarnessPartRepository.create(
        message_id=assistant.id, type="text", output=assistant.content
    )
    prompt = "Details " * 500 + "Keep the existing login API unchanged."
    HarnessMessageRepository.create(session_id=session.id, role="user", content=prompt)
    last = HarnessMessageRepository.create(session_id=session.id, role="assistant")
    material = _metadata_service(RoleProvider()).summary_material(session.id, last.id)
    assert prompt in material
    assert "Fixed the login bug in auth.py." in material


@pytest.mark.django_db
def test_summary_material_rejects_foreign_missing_or_user_message(
    harness_workspace, django_assert_num_queries
):
    """Only an assistant in the requested session can anchor the recap."""
    session, assistant = _session_with_answer(harness_workspace)
    other, _ = _session_with_answer(harness_workspace)
    service = _metadata_service(RoleProvider())
    user = HarnessMessageRepository.list_for_session(session.id)[0]
    for session_id, anchor_id in (
        (other.id, assistant.id),
        (session.id, uuid.uuid4()),
        (session.id, user.id),
    ):
        with django_assert_num_queries(1):
            assert service.summary_material(session_id, anchor_id) == ""


@pytest.mark.django_db
def test_summary_material_is_empty_without_requests(harness_workspace):
    """Do not invent a user goal for an empty transcript."""
    session, assistant = _session_with_answer(harness_workspace)
    user = HarnessMessageRepository.list_for_session(session.id)[0]
    user.content = ""
    user.save(update_fields=["content"])
    assert (
        _metadata_service(RoleProvider()).summary_material(session.id, assistant.id)
        == ""
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("harness_id", ["native", "claude"])
async def test_generate_summary_stores_sentence_for_every_engine(
    harness_workspace, harness_id
) -> None:
    """The org's small model summarizes native and external-engine sessions."""
    _configure_small_model(harness_workspace)
    session, assistant = _session_with_answer(harness_workspace, harness_id=harness_id)
    events: list[tuple[str, dict]] = []

    async def emit(event: str, data: dict) -> None:
        events.append((event, data))

    await _metadata_service(RoleProvider(), emit).generate_summary(
        session_id=session.id,
        assistant_id=assistant.id,
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )

    assert HarnessSession.objects.get(id=session.id).summary == SENTENCE
    assert events == [
        (
            "harness.session_summary",
            {
                "workspace_id": str(harness_workspace.id),
                "session_id": str(session.id),
                "summary": SENTENCE,
            },
        )
    ]


@pytest.mark.django_db
def test_session_out_carries_the_summary(harness_workspace):
    """The session API exposes the sentence for the chat view."""
    session, assistant = _session_with_answer(harness_workspace)
    assert session_to_out(session).summary == ""

    HarnessSessionRepository.set_summary_for_turn(session.id, assistant.id, SENTENCE)
    session.refresh_from_db()

    assert session_to_out(session).summary == SENTENCE


@pytest.mark.django_db(transaction=True)
async def test_generate_summary_needs_a_small_model(harness_workspace) -> None:
    """Without a configured small model nothing is generated or stored."""
    session, assistant = _session_with_answer(harness_workspace)
    provider = RoleProvider()

    await _metadata_service(provider).generate_summary(
        session_id=session.id,
        assistant_id=assistant.id,
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )

    assert provider.summary_inputs == []
    assert HarnessSession.objects.get(id=session.id).summary == ""


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("follower", ["title", "summary"])
@pytest.mark.parametrize(
    "outcome", ["error", "empty", "revoked_before", "revoked_after"]
)
async def test_metadata_failure_or_revocation_never_writes_or_emits(
    harness_workspace, monkeypatch, follower: str, outcome: str
) -> None:
    """Best-effort metadata cannot mutate a session after failure or revocation."""
    session, assistant = _session_with_answer(harness_workspace)
    events: list[tuple[str, dict]] = []

    async def emit(event: str, data: dict) -> None:
        events.append((event, data))

    runner = SimpleNamespace(
        run=AsyncMock(
            return_value=SimpleNamespace(
                output="" if outcome == "empty" else "Generated"
            )
        )
    )
    if outcome == "error":
        runner.run.side_effect = RuntimeError("Provider unavailable")
    build_runner = AsyncMock(return_value=(runner, "small-model"))
    monkeypatch.setattr(
        "apps.harness.session_metadata_service.build_small_model_runner", build_runner
    )
    reason = Mock(
        side_effect=["revoked"]
        if outcome == "revoked_before"
        else ["", "revoked"]
        if outcome == "revoked_after"
        else ["", "", ""]
    )
    monkeypatch.setattr(ProviderTurnService, "cancellation_reason", reason)
    service = _metadata_service(RoleProvider(), emit)
    kwargs = {
        "session_id": session.id,
        "organization_id": session.organization_id,
        "provider_turn_id": uuid.uuid4(),
    }
    if follower == "title":
        await service.generate_title(prompt="Fix login", **kwargs)
    else:
        await service.generate_summary(
            assistant_id=assistant.id, workspace_id=session.workspace_id, **kwargs
        )

    stored = await sync_to_async(HarnessSessionRepository.get_by_id)(session.id)
    assert stored.title == "Fix login"
    assert stored.summary == ""
    assert events == []
    if outcome == "revoked_before":
        build_runner.assert_not_awaited()
    else:
        runner.run.assert_awaited_once()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("follower", ["title", "summary"])
async def test_metadata_rechecks_session_after_generation(
    harness_workspace, monkeypatch, follower: str
) -> None:
    """A rename or newer answer during generation wins over its stale result."""
    session, assistant = _session_with_answer(harness_workspace)
    events: list[tuple[str, dict]] = []

    async def emit(event: str, data: dict) -> None:
        events.append((event, data))

    async def generate(*args):
        if follower == "title":
            await sync_to_async(HarnessSessionRepository.set_title)(session, "My title")
        else:
            await sync_to_async(HarnessMessageRepository.create)(
                session_id=session.id, role="assistant", content="New answer"
            )
        return SimpleNamespace(output="Stale metadata")

    runner = SimpleNamespace(run=AsyncMock(side_effect=generate))
    monkeypatch.setattr(
        "apps.harness.session_metadata_service.build_small_model_runner",
        AsyncMock(return_value=(runner, "small-model")),
    )
    service = _metadata_service(RoleProvider(), emit)
    kwargs = {"session_id": session.id, "organization_id": session.organization_id}
    if follower == "title":
        await service.generate_title(prompt="Fix login", **kwargs)
    else:
        await service.generate_summary(
            assistant_id=assistant.id, workspace_id=session.workspace_id, **kwargs
        )

    stored = await sync_to_async(HarnessSessionRepository.get_by_id)(session.id)
    assert stored.title == ("My title" if follower == "title" else "Fix login")
    assert stored.summary == ""
    assert events == []
    runner.run.assert_awaited_once()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("follower", ["title", "summary"])
async def test_metadata_registers_frozen_account_before_provider_request(
    harness_workspace, provider_account_factory, monkeypatch, follower: str
) -> None:
    """Shared runner composition keeps title/summary authority on the answer."""
    _configure_small_model(harness_workspace)
    account = provider_account_factory("openrouter")
    session, assistant = _session_with_answer(harness_workspace)
    turns = ProviderTurnService()
    account_ids = {"openrouter": str(account.id)}
    turn = turns.admit(
        assistant_message_id=assistant.id,
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
        account_ids=account_ids,
    )
    provider = RoleProvider()

    async def stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
        stored_turn = await sync_to_async(turns.get)(turn.id)
        assert stored_turn.used_account_ids == [str(account.id)]
        async for delta in provider.chat_stream(model, messages, tools, opts):
            yield delta

    monkeypatch.setattr(OpenRouterAdapter, "chat_stream", stream)
    monkeypatch.delenv("DJANGO_ALLOW_ASYNC_UNSAFE", raising=False)
    service = _metadata_service(None)
    authority = {
        "session_id": session.id,
        "organization_id": session.organization_id,
        "provider_turn_id": turn.id,
        "account_ids": account_ids,
        "initiated_by_id": harness_workspace.created_by_id,
    }
    if follower == "title":
        await service.generate_title(prompt="Fix login", **authority)
    else:
        await service.generate_summary(
            assistant_id=assistant.id, workspace_id=session.workspace_id, **authority
        )

    stored = await sync_to_async(HarnessSessionRepository.get_by_id)(session.id)
    if follower == "title":
        assert stored.title == "Login fix"
    else:
        assert stored.summary == SENTENCE


@pytest.mark.django_db(transaction=True)
async def test_run_writes_summary_and_next_run_clears_it(harness_workspace) -> None:
    """A finished run gets its sentence; starting the next run drops it again."""
    _configure_small_model(harness_workspace)
    provider = RoleProvider()
    service = _service(provider)
    org_id = harness_workspace.runner.organization_id
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=org_id,
        title="Fix the login",
        agent_name="build",
        mode="build",
        model="big-model",
    )

    async def run(prompt: str) -> None:
        await service.start_run(
            session,
            prompt,
            organization_id=org_id,
            workspace_id=str(harness_workspace.id),
            user_id=harness_workspace.created_by_id,
        )
        await service._tasks[str(session.id)]

    async def stored_summary() -> str:
        for _ in range(40):
            row = HarnessSession.objects.get(id=session.id)
            if row.summary:
                return row.summary
            await asyncio.sleep(0.05)
        return ""

    await run("Fix the login")
    assert await stored_summary() == SENTENCE
    assert "I fixed the login bug." in provider.summary_inputs[0]

    # No small model for the next run: the old sentence must not linger.
    ProviderConfigService().save_config(organization_id=org_id, small_model="")
    await run("And now the logout")
    await asyncio.sleep(0.2)
    assert HarnessSession.objects.get(id=session.id).summary == ""
