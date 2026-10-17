"""Tests for the one-sentence session status ("state + next step")."""

from __future__ import annotations

import asyncio

import pytest

import apps.harness.api as harness_api
from apps.harness.harness_service import (
    HarnessService,
    _normalize_generated_summary,
)
from apps.harness.models import HarnessMessageRole, HarnessSession, HarnessSessionStatus
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.permissions.service import PermissionService
from apps.harness.providers.base import Delta, ProviderAdapter, Usage
from apps.harness.repositories import (
    HarnessMessageRepository,
    HarnessSessionRepository,
    TodoRepositoryDjango,
)
from apps.harness.services import ProviderConfigService
from apps.harness.tools.todos import TodoItem

SENTENCE = "Login-Fehler behoben, als Nächstes die Änderung prüfen."


class RoleProvider(ProviderAdapter):
    """Answers the summary agent with a sentence, everything else with prose."""

    name = "fake"

    def __init__(self) -> None:
        self.summary_inputs: list[str] = []

    async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
        """Pick the answer by the agent's system prompt."""
        system = str(messages[0].content or "")
        if "status line" in system:
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


def _configure_small_model(workspace) -> None:
    ProviderConfigService().save_config(
        organization_id=workspace.runner.organization_id,
        api_key="test-key",
        base_url="https://example.com",
        default_model="big-model",
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


@pytest.mark.django_db
def test_summary_material_carries_request_answer_todos_and_failure(harness_workspace):
    """The summary agent sees the goal, the end of the answer and what is open."""
    session, assistant = _session_with_answer(
        harness_workspace, content="Done with step one.", finish="error", error="boom"
    )
    TodoRepositoryDjango().save(
        str(session.id),
        [
            TodoItem(content="write tests", status="pending"),
            TodoItem(content="fix bug", status="completed"),
        ],
    )

    material = _service(RoleProvider())._summary_material(session.id, assistant.id)

    assert "Fix the login" in material
    assert "Done with step one." in material
    assert "- write tests" in material
    assert "fix bug" not in material
    assert "The run failed: boom" in material


@pytest.mark.django_db
def test_summary_material_is_empty_without_answer_or_error(harness_workspace):
    """Nothing to say means no model call."""
    session, assistant = _session_with_answer(harness_workspace, content="")
    assert _service(RoleProvider())._summary_material(session.id, assistant.id) == ""


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

    await _service(RoleProvider(), emit)._generate_summary(
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
    assert harness_api._session_to_out(session).summary == ""

    HarnessSessionRepository.set_summary_for_turn(session.id, assistant.id, SENTENCE)
    session.refresh_from_db()

    assert harness_api._session_to_out(session).summary == SENTENCE


@pytest.mark.django_db(transaction=True)
async def test_generate_summary_needs_a_small_model(harness_workspace) -> None:
    """Without a configured small model nothing is generated or stored."""
    session, assistant = _session_with_answer(harness_workspace)
    provider = RoleProvider()

    await _service(provider)._generate_summary(
        session_id=session.id,
        assistant_id=assistant.id,
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )

    assert provider.summary_inputs == []
    assert HarnessSession.objects.get(id=session.id).summary == ""


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
    ProviderConfigService().save_config(
        organization_id=org_id,
        api_key="test-key",
        base_url="https://example.com",
        default_model="big-model",
        small_model="",
    )
    await run("And now the logout")
    await asyncio.sleep(0.2)
    assert HarnessSession.objects.get(id=session.id).summary == ""
