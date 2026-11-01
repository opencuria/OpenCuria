"""Durable API-to-execution commands with explicit live-operation boundaries."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from asgiref.sync import sync_to_async
from django.core.serializers.json import DjangoJSONEncoder

from common.exceptions import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    ServiceError,
)

from .command_models import HarnessCommand
from .command_repository import HarnessCommandRepository, SteeringSnapshotRepository
from .repositories import HarnessMessageRepository, HarnessSessionRepository
from .run_settings_snapshot import RunSettingsSnapshot
from .steering import SteerEntry

if TYPE_CHECKING:
    from .harness_service import HarnessService
    from .interaction_service import InteractionService


class HarnessCommandClient:
    """Wait for a durable reply; request cancellation never cancels committed work."""

    def __init__(
        self,
        *,
        repository: type[HarnessCommandRepository] = HarnessCommandRepository,
        interval: float = 0.05,
    ) -> None:
        self.repository = repository
        self.interval = interval

    async def call(
        self, kind: str, payload: dict, *, session_id: uuid.UUID | None = None
    ) -> Any:
        """Enqueue one operation and wait independently of the API process lifetime."""
        encoded = json.loads(json.dumps(payload, cls=DjangoJSONEncoder))
        admission = asyncio.create_task(
            sync_to_async(self.repository.enqueue)(kind, encoded, session_id)
        )
        row = await asyncio.shield(admission)
        while True:
            state = await sync_to_async(self.repository.get)(row.id)
            if state.state == HarnessCommand.State.COMPLETED:
                result = self.repository.decode_result(state)
                if not result["ok"]:
                    self._raise_error(result["error"])
                return result.get("value")
            await asyncio.sleep(self.interval)

    @staticmethod
    def _raise_error(error: dict) -> None:
        kind = error.get("type")
        message = error.get("message", "Execution request failed")
        if kind == "NotFoundError":
            raise NotFoundError(
                error.get("resource", "Resource"), error.get("identifier")
            )
        if kind == "AuthenticationError":
            raise AuthenticationError(message)
        if kind == "ConflictError":
            raise ConflictError(message, code=error.get("code", "conflict"))
        if kind == "ValueError":
            raise ValueError(message)
        raise ServiceError(message, code=error.get("code", "execution_failed"))


class HarnessCommandDispatcher:
    """Translate serialized requests to a fixed set of execution-service methods."""

    def __init__(self, service: HarnessService) -> None:
        self.service = service

    async def execute(self, kind: str, payload: dict) -> Any:
        """Rehydrate identities and invoke only allowed execution operations."""
        args = dict(payload)
        expected_settings = args.pop("expected_settings", None)
        session_id = (
            uuid.UUID(args.pop("session_id")) if args.get("session_id") else None
        )
        for key in (
            "organization_id",
            "message_id",
            "request_id",
            "question_id",
            "_edit_message_id",
        ):
            if args.get(key):
                args[key] = uuid.UUID(args[key])
        if kind == "abort_computeruse":
            rows = await self.service.abort_busy_computeruse_for_workspace(
                uuid.UUID(args["workspace_id"])
            )
            return [str(row.id) for row in rows]
        if kind not in {
            "start",
            "edit",
            "abort",
            "delete",
            "steer",
            "withdraw",
            "permission",
            "question",
        }:
            raise ValueError(f"Unknown harness command: {kind}")
        if session_id is None:
            raise ValueError("A session identity is required")
        if kind == "delete":
            await self.service.delete_session(session_id)
            return None
        if kind == "abort":
            return str((await self.service.abort_run(session_id)).id)
        session = await sync_to_async(self.service.conversations.get_session)(
            session_id
        )
        if kind in {"start", "edit"} and expected_settings is not None:
            RunSettingsSnapshot.from_payload(expected_settings).require_current(session)
        if kind == "edit":
            return str((await self.service.edit_user_message(session_id, **args)).id)
        if kind == "start":
            return str((await self.service.start_run(session, **args)).id)
        if kind == "steer":
            return (await self.service.steer_run(session, **args)).to_payload()
        if kind == "withdraw":
            return (await self.service.withdraw_steer(session, **args)).to_payload()
        if kind == "permission":
            return await self.service.interactions.resolve_permission(
                session=session, **args
            )
        return await self.service.interactions.resolve_question(session=session, **args)


class RemoteInteractions:
    """Read saved gates locally and resolve them in their waiter's execution process."""

    def __init__(self, reads: InteractionService, client: HarnessCommandClient) -> None:
        self._reads = reads
        self._client = client

    def list_pending_interactions(
        self, session_id: uuid.UUID, *, include_descendants: bool = False
    ) -> Any:
        """Use saved interaction rows for API and reconnect snapshots."""
        return self._reads.list_pending_interactions(
            session_id, include_descendants=include_descendants
        )

    async def resolve_permission(
        self, *, session: Any, request_id: uuid.UUID, response: str
    ) -> dict:
        """Resume the actual permission waiter through its execution owner."""
        return await self._client.call(
            "permission",
            {
                "session_id": session.id,
                "request_id": request_id,
                "response": response,
            },
            session_id=session.id,
        )

    async def resolve_question(
        self,
        *,
        session: Any,
        question_id: uuid.UUID,
        answers: list[Any],
        reject: bool = False,
    ) -> dict:
        """Resume the actual question waiter through its execution owner."""
        return await self._client.call(
            "question",
            {
                "session_id": session.id,
                "question_id": question_id,
                "answers": answers,
                "reject": reject,
            },
            session_id=session.id,
        )


class RemoteHarnessService:
    """API facade: saved conversation services locally, live operations via commands."""

    def __init__(
        self, reads: HarnessService, *, client: HarnessCommandClient | None = None
    ) -> None:
        self._reads = reads
        self.client = client or HarnessCommandClient()
        self.sessions = reads.sessions
        self.messages = reads.messages
        self.parts = reads.parts
        self.conversations = reads.conversations
        self.session_settings = reads.session_settings
        self.workspace_admission = reads.workspace_admission
        self.metadata = reads.metadata
        self.interactions = RemoteInteractions(reads.interactions, self.client)

    def create_session(self, **kwargs: Any) -> Any:
        """Create saved conversation metadata without starting an execution task."""
        return self._reads.create_session(**kwargs)

    def validate_provider_for_run(self, *args: Any, **kwargs: Any) -> Any:
        """Validate saved provider configuration without executing a run."""
        return self._reads.validate_provider_for_run(*args, **kwargs)

    def is_running(self, session_id: uuid.UUID) -> bool:
        """Read persisted activity across independent API and execution processes."""
        row = self.sessions.get_by_id(session_id)
        return row is not None and row.status == "busy"

    def list_pending_steers(self, session_id: uuid.UUID) -> list[SteerEntry]:
        """Read the execution owner's committed reconnect projection."""
        return [
            self._steer(entry) for entry in SteeringSnapshotRepository.get(session_id)
        ]

    @staticmethod
    def _steer(entry: dict) -> SteerEntry:
        return SteerEntry(
            id=entry["id"],
            content=entry["content"],
            created_at=datetime.fromisoformat(entry["created_at"]),
        )

    async def start_run(self, session: Any, prompt: str, **kwargs: Any) -> Any:
        """Persist an execution request before waiting for its assistant message."""
        result = await self.client.call(
            "start",
            {
                "session_id": session.id,
                "prompt": prompt,
                **kwargs,
                "expected_settings": RunSettingsSnapshot.capture(session).to_payload(),
            },
            session_id=session.id,
        )
        return await sync_to_async(HarnessMessageRepository.get_by_id)(
            uuid.UUID(result)
        )

    async def edit_user_message(self, session_id: uuid.UUID, **kwargs: Any) -> Any:
        """Perform edits and reruns under the execution owner's session fences."""
        session = await sync_to_async(self.conversations.get_session)(session_id)
        captured = RunSettingsSnapshot.capture(session)
        # Unspecified edit options refer to submission-time configuration, not
        # to defaults changed while the command waits for execution.
        options = {
            "mode": captured.mode,
            "model": captured.model,
            "reasoning_effort": captured.reasoning_effort,
            "skill_ids": list(captured.skill_ids),
        }
        for key, value in kwargs.items():
            # Optional API fields use None for an omitted override.
            if key in options and value is None:
                continue
            options[key] = value
        result = await self.client.call(
            "edit",
            {
                "session_id": session_id,
                **options,
                "expected_settings": captured.to_payload(),
            },
            session_id=session_id,
        )
        return await sync_to_async(HarnessMessageRepository.get_by_id)(
            uuid.UUID(result)
        )

    async def abort_run(self, session_id: uuid.UUID) -> Any:
        """Stop the running task tree in the independent execution process."""
        result = await self.client.call(
            "abort", {"session_id": session_id}, session_id=session_id
        )
        return await sync_to_async(HarnessSessionRepository.get_by_id)(
            uuid.UUID(result)
        )

    async def delete_session(self, session_id: uuid.UUID) -> None:
        """Join active execution before deleting its saved conversation."""
        await self.client.call(
            "delete", {"session_id": session_id}, session_id=session_id
        )

    async def steer_run(
        self, session: Any, prompt: str, *, user_id: int | None = None
    ) -> SteerEntry:
        """Queue a message at the active execution owner's next boundary."""
        return self._steer(
            await self.client.call(
                "steer",
                {"session_id": session.id, "prompt": prompt, "user_id": user_id},
                session_id=session.id,
            )
        )

    async def withdraw_steer(self, session: Any, steer_id: str) -> SteerEntry:
        """Withdraw an entry from its authoritative execution queue."""
        return self._steer(
            await self.client.call(
                "withdraw",
                {"session_id": session.id, "steer_id": steer_id},
                session_id=session.id,
            )
        )

    async def abort_busy_computeruse_for_workspace(
        self, workspace_id: uuid.UUID
    ) -> list[Any]:
        """Release computer-use tasks without touching their API worker's lifetime."""
        result = await self.client.call(
            "abort_computeruse", {"workspace_id": workspace_id}
        )
        return [
            await sync_to_async(self.sessions.get_by_id)(uuid.UUID(value))
            for value in result
        ]
