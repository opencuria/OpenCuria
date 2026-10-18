"""Integration tests for HarnessService's Claude engine orchestration seam."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest

from apps.harness.engines.claude.errors import ClaudeTranscriptError
from apps.harness.engines.connections import AuthConfiguration
from apps.harness.engines.run_repository import HarnessRunRepository
from apps.harness.engines.runs import HarnessRunService
from apps.harness.harness_service import (
    FRONTEND_EVENT_PART,
    HarnessService,
)
from apps.harness.models import (
    HarnessConnection,
    HarnessMessage,
    HarnessPart,
    HarnessSession,
)
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.permissions.models import PermissionAllowlist, PermissionRequest
from apps.harness.permissions.service import PermissionService
from apps.harness.providers.base import Usage
from apps.harness.repositories import HarnessMessageRepository
from apps.harness.tests.conftest import FakeAccessor
from common.exceptions import AuthenticationError, ConflictError


class FakeConnectionService:
    """Return opaque test auth material without consulting real credentials."""

    def __init__(self, connection_id: uuid.UUID) -> None:
        self.connection_id = connection_id
        self.resolutions: list[tuple[Any, Any, Any]] = []

    def resolve(self, organization_id, user_id, connection_id=None):  # type: ignore[no-untyped-def]
        self.resolutions.append((organization_id, user_id, connection_id))
        return AuthConfiguration(
            auth_type="api_token",
            token="do-not-persist-this-token",
            connection_id=self.connection_id,
        )


@pytest.mark.django_db(transaction=True)
async def test_closing_run_blocks_mutations_until_runner_release(harness_workspace):
    service, _registry, _connections, _events = _service(
        connection_id=uuid.uuid4(), behavior="cleanup_pending"
    )
    session = await _new_session(service, harness_workspace)
    assistant = await service.start_run(
        session,
        "leave recovery required",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]
    attempt = HarnessRunRepository.model.objects.get(assistant_message=assistant)
    assert attempt.status == "closing"

    service._accessor_factory = None
    messages_before = HarnessMessageRepository.list_for_session(session.id)
    user_message = messages_before[0]
    with pytest.raises(ConflictError):
        await service.edit_user_message(
            session.id,
            message_id=user_message.id,
            prompt="must not destructively edit",
            organization_id=session.organization_id,
            user_id=harness_workspace.created_by_id,
        )
    with pytest.raises(ConflictError):
        await service.delete_session(session.id)
    with pytest.raises(ConflictError):
        await service.abort_run(session.id)
    assert HarnessRunRepository.get_open_for_session(session.id) is not None
    assert HarnessMessageRepository.list_for_session(session.id) == messages_before
    assert HarnessSession.objects.filter(id=session.id).exists()

    # Recovery must positively confirm runner release before delete can cascade.
    recovery_accessor = FakeAccessor(
        str(session.workspace_id),
        desktop_result={"ok": True, "lease_state": "released"},
    )
    service._accessor_factory = lambda _workspace_id: recovery_accessor
    await service.delete_session(session.id)
    assert any(
        action == "release"
        for action, _args, _timeout in recovery_accessor.desktop_calls
    )
    assert not HarnessSession.objects.filter(id=session.id).exists()
    assert not HarnessRunRepository.model.objects.filter(id=attempt.id).exists()


@pytest.mark.django_db(transaction=True)
async def test_claude_run_uses_persisted_always_allowlist(harness_workspace):
    """Claude's shared ask callback skips an already allowlisted action."""
    permissions = PermissionService(
        evaluator=PermissionEvaluator(global_rules={"*": "ask"})
    )
    connection_id = uuid.uuid4()
    service, _registry, _connections, _events = _service(
        connection_id=connection_id,
        behavior="ask_permission",
        permissions=permissions,
    )
    session = await _new_session(service, harness_workspace)
    await service.start_run(
        session,
        "approve this action always",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    request = None
    for _ in range(50):
        request = await asyncio.to_thread(
            lambda: PermissionRequest.objects.filter(session_id=session.id).first()
        )
        if request is not None:
            break
        await asyncio.sleep(0.01)
    assert request is not None
    await service.resolve_permission(
        session=session, request_id=request.id, response="always"
    )
    first_task = service._tasks.get(str(session.id))
    if first_task is not None:
        await first_task

    assistant = await service.start_run(
        session,
        "reuse the persisted grant",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]

    assert assistant.finish == "stop"
    assert PermissionAllowlist.objects.filter(
        organization_id=session.organization_id,
        workspace_id=session.workspace_id,
        tool="edit",
        pattern="/workspace/src/file.py",
    ).exists()
    assert PermissionRequest.objects.filter(session_id=session.id).count() == 1


@pytest.mark.django_db(transaction=True)
async def test_claude_run_still_requests_nonmatching_allowlist_action(
    harness_workspace,
):
    """A persisted grant for one path does not authorize another path."""
    permissions = PermissionService(
        evaluator=PermissionEvaluator(global_rules={"*": "ask"})
    )
    service, _registry, _connections, _events = _service(
        connection_id=uuid.uuid4(),
        behavior="ask_permission_other",
        permissions=permissions,
    )
    session = await _new_session(service, harness_workspace)
    PermissionAllowlist.objects.create(
        organization_id=session.organization_id,
        workspace_id=session.workspace_id,
        tool="edit",
        pattern="/workspace/src/file.py",
    )

    await service.start_run(
        session,
        "ask for a different path",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    request = None
    for _ in range(50):
        request = await asyncio.to_thread(
            lambda: PermissionRequest.objects.filter(session_id=session.id).first()
        )
        if request is not None:
            break
        await asyncio.sleep(0.01)
    assert request is not None, "effective Claude ask did not create a request"
    assert request.status == "pending"
    assert request.pattern == "/workspace/src/other.py"

    await service.resolve_permission(
        session=session, request_id=request.id, response="once"
    )
    await service._tasks[str(session.id)]
    assert PermissionRequest.objects.filter(session_id=session.id).count() == 1


@pytest.mark.django_db(transaction=True)
async def test_claude_media_permission_callback_uses_shared_allowlist(
    harness_workspace,
):
    """External engines receive the shared permission_read authorization hook."""
    permissions = PermissionService(
        evaluator=PermissionEvaluator(global_rules={"read": "ask"})
    )
    service, _registry, _connections, _events = _service(
        connection_id=uuid.uuid4(), behavior="media_read", permissions=permissions
    )
    session = await _new_session(service, harness_workspace)
    PermissionAllowlist.objects.create(
        organization_id=session.organization_id,
        workspace_id=session.workspace_id,
        tool="read",
        pattern="/workspace/images/*.png",
    )

    assistant = await service.start_run(
        session,
        "inspect the image",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]

    assert assistant.finish == "stop"
    assert not PermissionRequest.objects.filter(session_id=session.id).exists()


@pytest.mark.django_db(transaction=True)
async def test_claude_max_steps_result_is_persisted_as_completed(harness_workspace):
    """A normal Claude turn-limit result remains completed with its reason."""
    service, _registry, _connections, _events = _service(
        connection_id=uuid.uuid4(), behavior="max_steps"
    )
    session = await _new_session(service, harness_workspace)

    assistant = await service.start_run(
        session,
        "reach the turn limit",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]

    attempt = HarnessRunRepository.model.objects.get(assistant_message=assistant)
    assert assistant.finish == "max_steps"
    assert assistant.error == ""
    assert attempt.status == "completed"


@pytest.mark.django_db(transaction=True)
async def test_claude_start_preflights_oversized_skill_before_messages(
    harness_workspace,
):
    """Claude skill limits fail before a user or assistant shell is created."""
    from apps.skills.models import Skill

    service, _registry, _connections, _events = _service(
        connection_id=uuid.uuid4()
    )
    session = await _new_session(service, harness_workspace)
    skill = Skill.objects.create(
        name="Oversized",
        body="x" * (64 * 1024 + 1),
        organization_id=session.organization_id,
        created_by_id=harness_workspace.created_by_id,
    )
    session.skill_ids = [str(skill.id)]
    session.save(update_fields=["skill_ids"])

    with pytest.raises(ValueError, match="skill body exceeds the managed size limit"):
        await service.start_run(
            session,
            "reject before admission",
            organization_id=session.organization_id,
            user_id=harness_workspace.created_by_id,
        )

    assert HarnessMessageRepository.list_for_session(session.id) == []
    session.refresh_from_db()
    assert session.status == "idle"


@pytest.mark.django_db(transaction=True)
async def test_claude_edit_preflights_inherited_skill_before_deleting_suffix(
    harness_workspace,
):
    """An invalid inherited selection preserves Claude history and transcript ID."""
    from apps.skills.models import Skill

    service, _registry, _connections, _events = _service(
        connection_id=uuid.uuid4()
    )
    session = await _new_session(service, harness_workspace)
    for prompt in ("first turn", "second turn"):
        await service.start_run(
            session,
            prompt,
            organization_id=session.organization_id,
            user_id=harness_workspace.created_by_id,
        )
        await service._tasks[str(session.id)]

    messages_before = HarnessMessageRepository.list_for_session(session.id)
    transcript_id_before = session.external_session_id
    assert transcript_id_before
    skill = Skill.objects.create(
        name="Oversized inherited skill",
        body="x" * (64 * 1024 + 1),
        organization_id=session.organization_id,
        created_by_id=harness_workspace.created_by_id,
    )
    session.skill_ids = [str(skill.id)]
    session.save(update_fields=["skill_ids"])
    target = next(message for message in messages_before if message.role == "user")

    with pytest.raises(ValueError, match="skill body exceeds the managed size limit"):
        await service.edit_user_message(
            session.id,
            message_id=target.id,
            prompt="must not delete history",
            organization_id=session.organization_id,
            user_id=harness_workspace.created_by_id,
        )

    assert HarnessMessageRepository.list_for_session(session.id) == messages_before
    session.refresh_from_db()
    assert session.external_session_id == transcript_id_before


@pytest.mark.django_db(transaction=True)
async def test_claude_async_run_works_without_async_unsafe_escape(
    harness_workspace, monkeypatch
):
    monkeypatch.delenv("DJANGO_ALLOW_ASYNC_UNSAFE", raising=False)
    service, _registry, _connections, _events = _service(connection_id=uuid.uuid4())
    session = await asyncio.to_thread(_new_session_sync, service, harness_workspace)
    assistant = await service.start_run(
        session,
        "no sync ORM on the event loop",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]
    assert assistant.finish == "stop"
    assert assistant.content == "Claude: done"
    fresh = await asyncio.to_thread(HarnessSession.objects.get, id=session.id)
    assert fresh.status == "idle"


def _new_session_sync(service, harness_workspace):
    """Create fixture-backed auth/session rows from an ORM worker thread."""
    connection_id = service._engine_connections.connection_id
    HarnessConnection.objects.create(
        id=connection_id,
        organization_id=harness_workspace.runner.organization_id,
        user_id=harness_workspace.created_by_id,
        auth_type="api_token",
        label="Claude test",
    )
    return service.create_session(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        prompt="Summarize the repository",
        mode="build",
        harness_id="claude",
        user_id=harness_workspace.created_by_id,
    )


class FakeClaudeEngine:
    """Small async engine that exercises runtime callbacks, never Claude SDK."""

    def __init__(self, *, behavior: str, kwargs: dict[str, Any]) -> None:
        self.behavior = behavior
        self.kwargs = kwargs

    async def _run_shell_task(
        self, agent, model, mode, opts
    ) -> None:  # type: ignore[no-untyped-def]
        """Exercise the real SDK message projector and Bash permission hooks."""
        from claude_agent_sdk._internal.message_parser import parse_message

        from apps.harness.agents.definitions import get_agent
        from apps.harness.engines.claude.engine import ClaudeEngine
        from apps.harness.engines.claude.permissionbridge import PermissionBridge

        engine = ClaudeEngine(
            tools=self.kwargs["tools"],
            accessor=self.kwargs["accessor"],
            emit=self.kwargs["emit"],
            on_child_event=self.kwargs["on_child_event"],
            evaluator=self.kwargs.get("evaluator"),
        )
        engine._options = opts
        engine._agent = get_agent(agent)
        engine._mode = mode
        engine._model = model
        engine._bridge = PermissionBridge(
            tools=engine.tools,
            evaluator=engine.evaluator,
            options=opts,
            agent=engine._agent,
            mode=mode,
            max_depth=opts.max_depth,
            engine=engine,
        )

        call_id = "local-bash-call"
        arguments = {
            "command": "printf 'local bash output'",
            "workdir": "/workspace",
        }
        pre_tool_use = engine._permission_hooks()["PreToolUse"][0].hooks[0]
        decision = await pre_tool_use(
            {"tool_name": "Bash", "tool_input": arguments}, call_id, None
        )
        assert decision["hookSpecificOutput"]["permissionDecision"] == "allow"

        async def handle(raw_message: dict[str, Any]) -> None:
            await engine._handle_message(parse_message(raw_message))

        await handle(
            {
                "type": "system",
                "subtype": "task_started",
                "task_id": "local-bash-task",
                "description": "Run a local shell command",
                "task_type": "local_bash",
                "tool_use_id": call_id,
                "uuid": "local-bash-started",
                "session_id": "claude-sdk-session",
            }
        )
        await handle(
            {
                "type": "system",
                "subtype": "task_progress",
                "task_id": "local-bash-task",
                "description": "Running command",
                "last_tool_name": "Bash",
                "usage": {},
                "tool_use_id": call_id,
                "uuid": "local-bash-progress",
                "session_id": "claude-sdk-session",
            }
        )
        await handle(
            {
                "type": "user",
                "uuid": "local-bash-result",
                "session_id": "claude-sdk-session",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": call_id,
                            "content": [{"type": "text", "text": "local bash output"}],
                        }
                    ],
                },
            }
        )
        await handle(
            {
                "type": "system",
                "subtype": "task_notification",
                "task_id": "local-bash-task",
                "tool_use_id": call_id,
                "status": "completed",
                "summary": "local bash output",
                "output_file": "",
                "uuid": "local-bash-notification",
                "session_id": "claude-sdk-session",
            }
        )
        await handle(
            {
                "type": "system",
                "subtype": "task_updated",
                "task_id": "local-bash-task",
                "tool_use_id": call_id,
                "patch": {"status": "completed", "result": "local bash output"},
                "uuid": "local-bash-updated",
                "session_id": "claude-sdk-session",
            }
        )

    async def run(self, prompt, agent, model, mode, opts):  # type: ignore[no-untyped-def]
        if self.behavior == "media_read":
            allowed = await opts.authorize_file_read(
                "/workspace/images/approved.png"
            )
            assert allowed is True
        if self.behavior in {"ask_permission", "ask_permission_other"}:
            action = (
                "/workspace/src/file.py"
                if self.behavior == "ask_permission"
                else "/workspace/src/other.py"
            )
            response = await opts.on_permission(
                tool="edit",
                action=action,
                title="Edit file",
                call_id="permission-call-1",
                key="permission",
            )
            assert response in {"once", "reject"}
        lease = {"lease_id": str(uuid.uuid4()), "epoch": "epoch-test"}
        external_id = self.kwargs.get("external_session_id") or str(uuid.uuid4())
        try:
            await self.kwargs["on_owner"](lease, "reserved")
            await self.kwargs["on_binding"](
                external_id,
                {
                    "harness_id": "claude",
                    "cli_version": "2.1.292",
                    "sdk_version": "0.2.164",
                    "model": model,
                    "mode": mode,
                },
            )
            if self.behavior == "shell_task":
                await self._run_shell_task(agent, model, mode, opts)
            else:
                await self.kwargs["emit"](
                    {
                        "type": "part_updated",
                        "step": 1,
                        "delta": {"text": "Claude: "},
                    }
                )
            if self.behavior in {"cleanup_pending", "closing_error"}:
                await self.kwargs["on_owner"](lease, "closing")
                raise RuntimeError("Claude failed while closing")
            if self.behavior == "subagent":
                await self.kwargs["emit"](
                    {
                        "type": "subtask_started",
                        "subtask_id": "sdk-task-1",
                        "parent_tool_use_id": "tool-call-1",
                        "agent": "explorer",
                        "description": "Inspect the code",
                        "model": model,
                    }
                )
                await self.kwargs["on_child_event"](
                    "tool-call-1",
                    {"type": "part_updated", "step": 1, "delta": {"text": "found it"}},
                )
                await self.kwargs["emit"](
                    {
                        "type": "subtask_finished",
                        "subtask_id": "sdk-task-1",
                        "parent_tool_use_id": "tool-call-1",
                        "agent": "explorer",
                        "status": "completed",
                        "summary": "found it",
                    }
                )
            if self.behavior == "steer":
                # The real engine streams queued messages to the CLI; the fake
                # just takes the next one like the engine's input feed does.
                assert opts.steering is not None
                assert await opts.steering.next() is not None
            if self.behavior == "error":
                raise RuntimeError("ANTHROPIC_API_KEY=do-not-persist-this-token")
            if self.behavior == "transcript_missing":
                raise ClaudeTranscriptError("is missing")
            if self.behavior == "transcript_oversized":
                raise ClaudeTranscriptError("exceeds the restore limit")
            if self.behavior == "cancel":
                await asyncio.Event().wait()
            await self.kwargs["emit"](
                {"type": "part_updated", "step": 1, "delta": {"text": "done"}}
            )
            from apps.harness.runner import RunResult

            return RunResult(
                output="Claude: done",
                steps=1,
                usage=Usage(3, 2, 5, 0.01),
                cost=0.01,
                finish_reason=(
                    "max_steps" if self.behavior == "max_steps" else "stop"
                ),
                metadata={
                    "harness_id": "claude",
                    "external_session_id": external_id,
                    "cli_version": "2.1.292",
                    "sdk_version": "0.2.164",
                    "model": model,
                    "reasoning_effort": "high",
                },
            )
        finally:
            if self.behavior not in {"cleanup_pending", "closing_error"}:
                await self.kwargs["on_owner"](lease, "released")


class FakeEngineRegistry:
    def __init__(self, behavior: str = "success") -> None:
        self.behavior = behavior
        self.created: list[tuple[str, dict[str, Any]]] = []

    def create(self, harness_id: str, **kwargs: Any) -> FakeClaudeEngine:
        self.created.append((harness_id, kwargs))
        return FakeClaudeEngine(behavior=self.behavior, kwargs=kwargs)


def _service(
    *,
    connection_id: uuid.UUID,
    behavior: str = "success",
    permissions: PermissionService | None = None,
):
    emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit(name: str, data: dict[str, Any]) -> None:
        emitted.append((name, dict(data)))

    registry = FakeEngineRegistry(behavior)
    connections = FakeConnectionService(connection_id)
    service = HarnessService(
        emit=emit,
        permissions=permissions,
        accessor_factory=lambda workspace_id: _fake_accessor(workspace_id),
        engine_registry=registry,
        engine_connections=connections,
        run_service=HarnessRunService(),
    )
    return service, registry, connections, emitted


async def _fake_accessor(workspace_id: str) -> FakeAccessor:
    return FakeAccessor(workspace_id)


async def _new_session(service, harness_workspace, *, harness_id="claude"):
    connection_id = service._engine_connections.connection_id
    HarnessConnection.objects.create(
        id=connection_id,
        organization_id=harness_workspace.runner.organization_id,
        user_id=harness_workspace.created_by_id,
        auth_type="api_token",
        label="Claude test",
    )
    return service.create_session(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        prompt="Summarize the repository",
        mode="build",
        harness_id=harness_id,
        user_id=harness_workspace.created_by_id,
    )


@pytest.mark.django_db(transaction=True)
async def test_claude_run_persists_engine_binding_and_secret_free_result(
    harness_workspace,
):
    connection_id = uuid.uuid4()
    service, registry, connections, events = _service(connection_id=connection_id)
    session = await _new_session(service, harness_workspace)

    assert session.harness_id == "claude"
    assert session.connection_id == connection_id
    assert session.created_by_id == harness_workspace.created_by_id
    assert session.model == "sonnet"
    assert session.reasoning_effort == "high"
    assert connections.resolutions[0][1] == harness_workspace.created_by_id

    assistant = await service.start_run(
        session,
        "Summarize the repository",
        organization_id=session.organization_id,
        workspace_id=str(session.workspace_id),
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]

    await asyncio.to_thread(assistant.refresh_from_db)
    await asyncio.to_thread(session.refresh_from_db)
    assert assistant.content == "Claude: done"
    assert assistant.harness_id == "claude"
    assert assistant.engine_meta["harness_id"] == "claude"
    assert "do-not-persist-this-token" not in str(assistant.engine_meta)
    assert session.external_session_id == assistant.engine_meta["external_session_id"]
    assert session.status == "idle"
    attempt = HarnessRunRepository.get_open_for_session(session.id)
    assert attempt is None
    assert registry.created[0][0] == "claude"
    assert registry.created[0][1]["auth_env"] == {
        "ANTHROPIC_API_KEY": "do-not-persist-this-token"
    }
    assert any(name == FRONTEND_EVENT_PART for name, _ in events)
    assert all("do-not-persist-this-token" not in str(data) for _, data in events)


@pytest.mark.django_db(transaction=True)
async def test_claude_fork_and_edit_never_resume_stale_cli_suffix(
    harness_workspace,
):
    service, registry, _connections, _events = _service(connection_id=uuid.uuid4())
    session = await _new_session(service, harness_workspace)
    first_assistant = await service.start_run(
        session,
        "first prompt",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]
    first_assistant.refresh_from_db()
    user_message = HarnessMessageRepository.list_for_session(session.id)[0]
    original_external_id = session.external_session_id
    assert original_external_id

    forked = await service.fork_session(session.id)
    forked.refresh_from_db()
    assert forked.harness_id == "claude"
    assert forked.connection_id == session.connection_id
    assert forked.created_by_id == session.created_by_id
    assert forked.external_session_id == ""
    assert forked.engine_state == {}
    copied = HarnessMessageRepository.list_for_session(forked.id)
    assert copied[-1].engine_meta == first_assistant.engine_meta
    assert copied[-1].harness_id == "claude"

    await service.edit_user_message(
        session.id,
        message_id=user_message.id,
        prompt="edited prompt",
        organization_id=session.organization_id,
        workspace_id=str(session.workspace_id),
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]
    session.refresh_from_db()
    assert session.external_session_id != original_external_id
    assert registry.created[-1][1]["external_session_id"] == ""
    assert (
        HarnessMessageRepository.list_for_session(session.id)[0].content
        == "edited prompt"
    )


@pytest.mark.django_db(transaction=True)
async def test_claude_closing_callbacks_still_finalize_success_and_error(
    harness_workspace,
):
    service, _registry, _connections, _events = _service(
        connection_id=uuid.uuid4(), behavior="closing_result"
    )
    session = await _new_session(service, harness_workspace)
    assistant = await service.start_run(
        session,
        "closing result",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]
    assistant.refresh_from_db()
    session.refresh_from_db()
    row = HarnessRunRepository.model.objects.get(assistant_message=assistant)
    assert row.status == "completed"
    assert assistant.finish == "stop"
    assert assistant.content == "Claude: done"
    assert session.status == "idle"

    from django.contrib.auth import get_user_model

    from apps.runners.enums import RunnerStatus, WorkspaceStatus
    from apps.runners.models import Runner, Workspace
    from common.utils import hash_token

    user = get_user_model().objects.create_user(
        email=f"closing-{uuid.uuid4().hex[:8]}@example.com", password="secret"
    )
    runner = Runner.objects.create(
        name="closing-runner",
        api_token_hash=hash_token(uuid.uuid4().hex),
        status=RunnerStatus.ONLINE,
        sid=f"closing-{uuid.uuid4().hex[:8]}",
        organization_id=harness_workspace.runner.organization_id,
        available_runtimes=["docker"],
    )
    second_workspace = Workspace.objects.create(
        runner=runner,
        name="Closing Workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=user,
    )
    failed_service, _registry, _connections, _events = _service(
        connection_id=uuid.uuid4(), behavior="closing_error"
    )
    failed_session = await _new_session(failed_service, second_workspace)
    failed_assistant = await failed_service.start_run(
        failed_session,
        "closing error",
        organization_id=failed_session.organization_id,
        user_id=second_workspace.created_by_id,
    )
    await failed_service._tasks[str(failed_session.id)]
    failed_assistant.refresh_from_db()
    failed_session.refresh_from_db()
    failed_row = HarnessRunRepository.model.objects.get(
        assistant_message=failed_assistant
    )
    assert failed_row.status == "closing"
    assert failed_assistant.finish == "error"
    assert failed_assistant.error == "Claude run failed."
    assert failed_session.status == "busy"


@pytest.mark.django_db(transaction=True)
async def test_claude_unconfirmed_cleanup_retains_run_admission(harness_workspace):
    service, _registry, _connections, _events = _service(
        connection_id=uuid.uuid4(), behavior="cleanup_pending"
    )
    session = await _new_session(service, harness_workspace)
    assistant = await service.start_run(
        session,
        "cleanup must remain fenced",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]

    row = HarnessRunRepository.model.objects.get(assistant_message=assistant)
    assistant.refresh_from_db()
    session.refresh_from_db()
    assert row.status == "closing"
    assert assistant.finish == "error"
    assert assistant.error == "Claude run failed."
    assert session.status == "busy"
    assert not service.is_running(session.id)


@pytest.mark.django_db(transaction=True)
async def test_claude_session_auth_is_owner_bound(harness_workspace):
    connection_id = uuid.uuid4()
    service, _registry, _connections, _events = _service(connection_id=connection_id)
    session = await _new_session(service, harness_workspace)

    with pytest.raises(AuthenticationError):
        await service.start_run(
            session,
            "not the owner",
            organization_id=session.organization_id,
            user_id=harness_workspace.created_by_id + 1000,
        )
    assert HarnessMessageRepository.list_for_session(session.id) == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("behavior", "reason"),
    [
        ("transcript_missing", "is missing"),
        ("transcript_oversized", "exceeds the restore limit"),
    ],
)
async def test_claude_transcript_error_surfaces_safe_recovery_action(
    harness_workspace, behavior, reason
):
    """Only typed transcript errors expose their safe actionable message."""
    service, _registry, _connections, _events = _service(
        connection_id=uuid.uuid4(), behavior=behavior
    )
    session = await _new_session(service, harness_workspace)
    assistant = await service.start_run(
        session,
        "resume stored conversation",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]

    assistant.refresh_from_db()
    assert assistant.finish == "error"
    assert assistant.error == str(ClaudeTranscriptError(reason))
    assert "Fork this conversation or start a new session" in assistant.error
    attempt = HarnessRunRepository.model.objects.get(assistant_message=assistant)
    assert attempt.status == "error"


@pytest.mark.django_db(transaction=True)
async def test_claude_engine_error_is_generic_in_message_and_terminal_attempt(
    harness_workspace,
    caplog,
):
    service, _registry, _connections, _events = _service(
        connection_id=uuid.uuid4(), behavior="error"
    )
    session = await _new_session(service, harness_workspace)
    assistant = await service.start_run(
        session,
        "fail safely",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]

    assistant.refresh_from_db()
    assert assistant.finish == "error"
    assert assistant.error == "Claude run failed."
    assert "do-not-persist-this-token" not in assistant.error
    assert "do-not-persist-this-token" not in caplog.text
    attempt = HarnessRunRepository.model.objects.get(assistant_message=assistant)
    assert attempt.status == "error"
    assert attempt.error == "Engine run failed."


@pytest.mark.django_db(transaction=True)
async def test_claude_local_bash_task_lifecycle_stays_in_root_tool_part(
    harness_workspace,
):
    service, _registry, _connections, events = _service(
        connection_id=uuid.uuid4(), behavior="shell_task"
    )
    session = await _new_session(service, harness_workspace)

    assistant = await service.start_run(
        session,
        "run a local bash command",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]

    assistant.refresh_from_db()
    bash_part = HarnessPart.objects.get(
        message=assistant, type="tool", call_id="local-bash-call"
    )
    assert bash_part.input["tool"] == "bash"
    assert bash_part.state == "completed"
    assert bash_part.output == "local bash output"
    bash_part_events = [
        data.get("delta", {})
        for name, data in events
        if name == FRONTEND_EVENT_PART
        and data.get("session_id") == str(session.id)
        and data.get("delta", {}).get("call_id") == "local-bash-call"
    ]
    assert any(
        delta.get("tool_started") == "bash" and delta.get("state") == "pending"
        for delta in bash_part_events
    )
    assert any(
        delta.get("tool_started") == "bash" and delta.get("state") == "running"
        for delta in bash_part_events
    )
    assert any(
        delta.get("tool_completed") == "bash" and delta.get("state") == "completed"
        for delta in bash_part_events
    )
    assert not HarnessSession.objects.filter(parent_id=session.id).exists()
    assert not HarnessPart.objects.filter(message=assistant, type="subtask").exists()
    assert not any(name.startswith("harness.subtask_") for name, _data in events)
    attempt = HarnessRunRepository.model.objects.get(assistant_message=assistant)
    assert attempt.status == "completed"


@pytest.mark.django_db(transaction=True)
async def test_claude_abort_finishes_attempt_and_child_projection(harness_workspace):
    service, _registry, _connections, events = _service(
        connection_id=uuid.uuid4(), behavior="subagent"
    )
    session = await _new_session(service, harness_workspace)
    assistant = await service.start_run(
        session,
        "delegate work",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]

    child = HarnessSession.objects.get(parent_id=session.id)
    child_assistant = HarnessMessage.objects.filter(
        session=child, role="assistant"
    ).get()
    assert child.harness_id == "claude"
    assert child.agent_name == "explore"
    assert child.status == "idle"
    assert child_assistant.content == "found it"
    assert HarnessPart.objects.filter(
        message=assistant, type="subtask", meta__child_session_id=str(child.id)
    ).exists()
    assert any(name == "harness.subtask_started" for name, _ in events)
    assert any(name == "harness.subtask_finished" for name, _ in events)


@pytest.mark.django_db(transaction=True)
async def test_queued_tool_card_is_idempotent_and_enriched_by_later_payload(
    harness_workspace,
):
    service, _registry, _connections, events = _service(connection_id=uuid.uuid4())
    session = await _new_session(service, harness_workspace)
    await asyncio.to_thread(service.sessions.mark_status, session, "busy")
    assistant = await asyncio.to_thread(
        service.messages.create,
        session_id=session.id,
        role="assistant",
        content="",
        harness_id="claude",
    )
    service._runs[str(session.id)] = {
        "session_id": str(session.id),
        "message_id": str(assistant.id),
        "tool_parts": {},
        "step_parts": {},
        "subtask_parts": {},
        "skill_bodies": [],
    }

    early = {
        "type": "tool_queued",
        "step": 1,
        "call_id": "call-queued-1",
        "tool": "read",
        "title": "Read file",
        "arguments": "",
    }
    await service._on_runner_event(session, assistant, early)
    first = await asyncio.to_thread(
        lambda: HarnessPart.objects.get(
            message_id=assistant.id, call_id="call-queued-1"
        )
    )
    await service._on_runner_event(session, assistant, early)
    await service._on_runner_event(
        session,
        assistant,
        {
            **early,
            "title": "Read /workspace/README.md",
            "arguments": json.dumps({"path": "/workspace/README.md"}),
        },
    )
    rows = await asyncio.to_thread(
        lambda: list(
            HarnessPart.objects.filter(
                message_id=assistant.id, type="tool", call_id="call-queued-1"
            )
        )
    )
    assert len(rows) == 1
    assert rows[0].id == first.id
    assert rows[0].state == "pending"
    assert rows[0].title == "Read /workspace/README.md"
    assert rows[0].input == {
        "tool": "read",
        "arguments": json.dumps({"path": "/workspace/README.md"}),
    }

    await service._on_runner_event(
        session,
        assistant,
        {
            "type": "tool_started",
            "step": 1,
            "call_id": "call-queued-1",
            "tool": "read",
            "title": "Read /workspace/README.md",
            "arguments": json.dumps({"path": "/workspace/README.md"}),
        },
    )
    await service._on_runner_event(
        session,
        assistant,
        {
            "type": "tool_completed",
            "step": 1,
            "call_id": "call-queued-1",
            "tool": "read",
            "output": "read complete",
        },
    )
    await service._on_runner_event(
        session,
        assistant,
        {
            **early,
            "title": "late queued notification",
            "arguments": '{"path":"stale"}',
        },
    )
    rows = await asyncio.to_thread(
        lambda: list(
            HarnessPart.objects.filter(
                message_id=assistant.id, type="tool", call_id="call-queued-1"
            )
        )
    )
    assert len(rows) == 1
    assert rows[0].id == first.id
    assert rows[0].state == "completed"
    assert rows[0].input["arguments"] == json.dumps({"path": "/workspace/README.md"})
    assert rows[0].output == "read complete"
    correlated_events = [
        data
        for name, data in events
        if name == FRONTEND_EVENT_PART
        and (
            data.get("delta", {}).get("call_id") == "call-queued-1"
            or data.get("part_id") == str(first.id)
        )
        and data.get("delta", {}).get("tool_started") == "read"
    ]
    assert correlated_events
    assert {data.get("part_id") for data in correlated_events} == {str(first.id)}


@pytest.mark.django_db(transaction=True)
async def test_claude_cancellation_sets_interrupted_attempt(harness_workspace):
    service, _registry, _connections, _events = _service(
        connection_id=uuid.uuid4(), behavior="cancel"
    )
    session = await _new_session(service, harness_workspace)
    assistant = await service.start_run(
        session,
        "wait for cancellation",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    task = service._tasks[str(session.id)]
    await asyncio.sleep(0.05)
    await service.abort_run(session.id)
    if not task.done():
        await task

    assistant.refresh_from_db()
    assert assistant.finish == "aborted"
    attempt = HarnessRunRepository.model.objects.get(assistant_message=assistant)
    assert attempt.status == "interrupted"
    session.refresh_from_db()
    assert session.status == "idle"


@pytest.mark.django_db(transaction=True)
async def test_claude_run_hands_the_steering_channel_to_the_engine(
    harness_workspace,
):
    """A message sent mid-run reaches the engine and lands between the texts."""
    service, _registry, _connections, events = _service(
        connection_id=uuid.uuid4(), behavior="steer"
    )
    session = await _new_session(service, harness_workspace)
    await service.start_run(
        session,
        "start working",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    entry = await service.steer_run(
        session, "also add tests", user_id=harness_workspace.created_by_id
    )
    await service._tasks[str(session.id)]

    parts = list(
        HarnessPart.objects.filter(message__session_id=session.id).order_by(
            "message__position", "position"
        )
    )
    kinds = [part.type for part in parts]
    steer_index = kinds.index("steer")
    assert "text" in kinds[:steer_index] and "text" in kinds[steer_index + 1 :]
    assert parts[steer_index].output == "also add tests"
    assert parts[steer_index].call_id == entry.id
    assert service.list_pending_steers(session.id) == []
    assert any(
        name == FRONTEND_EVENT_PART and data["delta"].get("steer") == "also add tests"
        for name, data in events
    )
    fresh = HarnessSession.objects.get(id=session.id)
    assert fresh.status == "idle"
