"""
Repository layer for the harness app.

Encapsulates all database queries. Services never use the ORM directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from django.utils import timezone

from .models import (
    HarnessMessage,
    HarnessPart,
    HarnessSession,
    HarnessSessionStatus,
    ProviderConfig,
    ProviderConnection,
    QuestionRequest,
    QuestionRequestStatus,
    Todo,
)


class ProviderConfigRepository:
    """Data access for ProviderConfig records."""

    @staticmethod
    def get_by_org(org_id: uuid.UUID) -> ProviderConfig | None:
        """Fetch the provider config for an organization."""
        return ProviderConfig.objects.filter(organization_id=org_id).first()

    @staticmethod
    def get_by_id(config_id: uuid.UUID) -> ProviderConfig | None:
        """Fetch a provider config by ID."""
        return ProviderConfig.objects.filter(id=config_id).first()

    @staticmethod
    def create(
        *,
        organization_id: uuid.UUID,
        default_model: str = "",
        small_model: str = "",
        computer_use_model: str = "",
        default_effort: str = "",
        small_effort: str = "",
        computer_use_effort: str = "",
    ) -> ProviderConfig:
        """Create a provider config for an organization."""
        return ProviderConfig.objects.create(
            organization_id=organization_id,
            default_model=default_model,
            small_model=small_model,
            computer_use_model=computer_use_model,
            default_effort=default_effort,
            small_effort=small_effort,
            computer_use_effort=computer_use_effort,
        )

    @staticmethod
    def update(
        config: ProviderConfig,
        *,
        default_model: str | None = None,
        small_model: str | None = None,
        computer_use_model: str | None = None,
        default_effort: str | None = None,
        small_effort: str | None = None,
        computer_use_effort: str | None = None,
    ) -> ProviderConfig:
        """Update provider config fields."""
        update_fields = ["updated_at"]
        if default_model is not None:
            config.default_model = default_model
            update_fields.append("default_model")
        if small_model is not None:
            config.small_model = small_model
            update_fields.append("small_model")
        if computer_use_model is not None:
            config.computer_use_model = computer_use_model
            update_fields.append("computer_use_model")
        if default_effort is not None:
            config.default_effort = default_effort
            update_fields.append("default_effort")
        if small_effort is not None:
            config.small_effort = small_effort
            update_fields.append("small_effort")
        if computer_use_effort is not None:
            config.computer_use_effort = computer_use_effort
            update_fields.append("computer_use_effort")
        config.save(update_fields=update_fields)
        return config

    @staticmethod
    def delete_by_org(org_id: uuid.UUID) -> int:
        """Delete the provider config for an organization."""
        count, _ = ProviderConfig.objects.filter(organization_id=org_id).delete()
        return count


class ProviderConnectionRepository:
    """Data access for ProviderConnection records."""

    @staticmethod
    def get_by_org_and_provider(
        organization_id: uuid.UUID,
        provider: str,
    ) -> ProviderConnection | None:
        """Fetch a provider connection for an organization and provider."""
        return ProviderConnection.objects.filter(
            organization_id=organization_id,
            provider=provider,
        ).first()

    @staticmethod
    def list_by_org(organization_id: uuid.UUID) -> list[ProviderConnection]:
        """List all provider connections for an organization."""
        return list(
            ProviderConnection.objects.filter(
                organization_id=organization_id,
            ).order_by("provider")
        )

    @staticmethod
    def upsert(
        organization_id: uuid.UUID,
        provider: str,
        credentials_encrypted: str,
        config: dict | None = None,
    ) -> ProviderConnection:
        """Create or update a provider connection for an organization."""
        connection, _ = ProviderConnection.objects.update_or_create(
            organization_id=organization_id,
            provider=provider,
            defaults={
                "credentials_encrypted": credentials_encrypted,
                "config": dict(config or {}),
            },
        )
        return connection

    @staticmethod
    def update_credentials(
        organization_id: uuid.UUID,
        provider: str,
        credentials_encrypted: str,
    ) -> ProviderConnection | None:
        """Update stored credentials for a provider connection."""
        connection = ProviderConnection.objects.filter(
            organization_id=organization_id,
            provider=provider,
        ).first()
        if connection is None:
            return None
        connection.credentials_encrypted = credentials_encrypted
        connection.save(update_fields=["credentials_encrypted", "updated_at"])
        return connection

    @staticmethod
    def delete_by_org_and_provider(
        organization_id: uuid.UUID,
        provider: str,
    ) -> bool:
        """Delete a provider connection; return whether a row was removed."""
        deleted, _ = ProviderConnection.objects.filter(
            organization_id=organization_id,
            provider=provider,
        ).delete()
        return deleted > 0


class HarnessSessionRepository:
    """Data access for HarnessSession records."""

    model = HarnessSession

    @staticmethod
    def create(
        *,
        workspace_id: uuid.UUID,
        organization_id: uuid.UUID,
        title: str = "",
        mode: str = "build",
        agent_name: str = "build",
        model: str = "",
        reasoning_effort: str = "",
        parent_id: uuid.UUID | None = None,
        skill_ids: list[str] | None = None,
    ) -> HarnessSession:
        """Create a harness session bound to a workspace."""
        return HarnessSession.objects.create(
            workspace_id=workspace_id,
            organization_id=organization_id,
            title=title or "",
            mode=mode,
            agent_name=agent_name,
            model=model or "",
            reasoning_effort=reasoning_effort or "",
            parent_id=parent_id,
            skill_ids=list(skill_ids or []),
        )

    @staticmethod
    def get_by_id(session_id: uuid.UUID) -> HarnessSession | None:
        """Fetch a session by ID."""
        return HarnessSession.objects.filter(id=session_id).first()

    @staticmethod
    def get_for_workspace(
        session_id: uuid.UUID,
        workspace_id: uuid.UUID,
    ) -> HarnessSession | None:
        """Fetch a session only when it belongs to *workspace_id*."""
        return HarnessSession.objects.filter(
            id=session_id, workspace_id=workspace_id
        ).first()

    @staticmethod
    def list_for_workspace(workspace_id: uuid.UUID) -> list[HarnessSession]:
        """Return all sessions of a workspace ordered by creation."""
        return list(
            HarnessSession.objects.filter(workspace_id=workspace_id).order_by(
                "created_at"
            )
        )

    @staticmethod
    def list_children(parent_id: uuid.UUID) -> list[HarnessSession]:
        """Return child sessions spawned from *parent_id*."""
        return list(
            HarnessSession.objects.filter(parent_id=parent_id).order_by("created_at")
        )

    @staticmethod
    def list_descendant_ids(session_id: uuid.UUID) -> list[uuid.UUID]:
        """Return *session_id* followed by descendant ids (breadth-first)."""
        ids: list[uuid.UUID] = [session_id]
        queue: list[uuid.UUID] = [session_id]
        seen: set[uuid.UUID] = {session_id}
        while queue:
            current = queue.pop(0)
            children = list(
                HarnessSession.objects.filter(parent_id=current).values_list(
                    "id", flat=True
                )
            )
            for child_id in children:
                if child_id in seen:
                    continue
                seen.add(child_id)
                ids.append(child_id)
                queue.append(child_id)
        return ids

    @staticmethod
    def list_by_ids(session_ids: list[uuid.UUID]) -> list[HarnessSession]:
        """Return sessions for *session_ids* (order not guaranteed)."""
        if not session_ids:
            return []
        return list(HarnessSession.objects.filter(id__in=session_ids))

    @staticmethod
    def list_busy_computeruse_for_workspace(
        workspace_id: uuid.UUID,
    ) -> list[HarnessSession]:
        """Return busy computer-use sessions for *workspace_id*."""
        return list(
            HarnessSession.objects.filter(
                workspace_id=workspace_id,
                agent_name="computeruse",
                status=HarnessSessionStatus.BUSY,
            ).order_by("created_at")
        )

    @staticmethod
    def list_for_workspaces(workspace_ids: list[uuid.UUID]) -> list[HarnessSession]:
        """Return root sessions across *workspace_ids* (newest first)."""
        if not workspace_ids:
            return []
        return list(
            HarnessSession.objects.filter(
                workspace_id__in=workspace_ids,
                parent__isnull=True,
            ).order_by("-updated_at")
        )

    @staticmethod
    def list_id_parent_for_workspaces(
        workspace_ids: list[uuid.UUID],
    ) -> list[tuple[uuid.UUID, uuid.UUID | None]]:
        """Return ``(id, parent_id)`` for every session in *workspace_ids*."""
        if not workspace_ids:
            return []
        return list(
            HarnessSession.objects.filter(workspace_id__in=workspace_ids).values_list(
                "id", "parent_id"
            )
        )

    @staticmethod
    def get_root_id(session: HarnessSession) -> uuid.UUID:
        """Return the top-level session id for *session*."""
        current_id = session.id
        parent_id = session.parent_id
        seen: set[uuid.UUID] = {current_id}
        while parent_id is not None:
            if parent_id in seen:
                break
            seen.add(parent_id)
            parent = (
                HarnessSession.objects.filter(id=parent_id)
                .only("id", "parent_id")
                .first()
            )
            if parent is None:
                break
            current_id = parent.id
            parent_id = parent.parent_id
        return current_id

    @staticmethod
    def mark_read(session: HarnessSession) -> HarnessSession:
        """Record that the user opened this session."""
        session.last_read_at = timezone.now()
        session.manual_unread_at = None
        session.save(update_fields=["last_read_at", "manual_unread_at", "updated_at"])
        return session

    @staticmethod
    def mark_unread(session: HarnessSession) -> HarnessSession:
        """Record that the user explicitly marked this session unread."""
        session.manual_unread_at = timezone.now()
        session.save(update_fields=["manual_unread_at", "updated_at"])
        return session

    @staticmethod
    def set_title(session: HarnessSession, title: str) -> HarnessSession:
        """Persist a session title."""
        session.title = (title or "").strip()[:255]
        session.save(update_fields=["title", "updated_at"])
        return session

    @staticmethod
    def set_skill_ids(session: HarnessSession, skill_ids: list[str]) -> HarnessSession:
        """Persist selected skill IDs for subsequent runs."""
        session.skill_ids = list(skill_ids or [])
        session.save(update_fields=["skill_ids", "updated_at"])
        return session

    @staticmethod
    def delete(session: HarnessSession) -> None:
        """Delete a harness session and its related rows."""
        session.delete()

    @staticmethod
    def mark_status(session: HarnessSession, status: str) -> HarnessSession:
        """Persist a session status change (busy|idle)."""
        session.status = status
        session.save(update_fields=["status", "updated_at"])
        return session

    @staticmethod
    def set_mode(session: HarnessSession, mode: str) -> HarnessSession:
        """Persist mode (plan|build) and align primary agent_name when applicable."""
        session.mode = mode
        update_fields = ["mode", "updated_at"]
        if mode in ("plan", "build"):
            session.agent_name = mode
            update_fields.append("agent_name")
        session.save(update_fields=update_fields)
        return session

    @staticmethod
    def set_model(session: HarnessSession, model: str) -> HarnessSession:
        """Persist the session model override for subsequent runs."""
        session.model = (model or "").strip()
        session.save(update_fields=["model", "updated_at"])
        return session

    @staticmethod
    def set_reasoning_effort(
        session: HarnessSession, reasoning_effort: str
    ) -> HarnessSession:
        """Persist the OpenRouter reasoning effort for subsequent runs."""
        session.reasoning_effort = (reasoning_effort or "").strip()
        session.save(update_fields=["reasoning_effort", "updated_at"])
        return session

    @staticmethod
    def add_usage(
        session: HarnessSession,
        *,
        cost: float = 0.0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> HarnessSession:
        """Accumulate cost/tokens counters on a session."""
        tokens = dict(session.tokens or {})
        tokens["prompt"] = int(tokens.get("prompt", 0)) + prompt_tokens
        tokens["completion"] = int(tokens.get("completion", 0)) + (completion_tokens)
        tokens["total"] = int(tokens.get("total", 0)) + total_tokens
        session.tokens = tokens
        session.cost = float(session.cost or 0.0) + float(cost or 0.0)
        session.save(update_fields=["tokens", "cost", "updated_at"])
        return session


class HarnessMessageRepository:
    """Data access for HarnessMessage records."""

    model = HarnessMessage

    @staticmethod
    def create(
        *,
        session_id: uuid.UUID,
        role: str,
        content: str = "",
        model: str = "",
        reasoning_effort: str = "",
        provider: str = "",
    ) -> HarnessMessage:
        """Create a user or assistant message shell."""
        return HarnessMessage.objects.create(
            session_id=session_id,
            role=role,
            content=content or "",
            model=model or "",
            reasoning_effort=reasoning_effort or "",
            provider=provider or "",
        )

    @staticmethod
    def list_for_session(session_id: uuid.UUID) -> list[HarnessMessage]:
        """Return all messages of a session ordered by creation."""
        return list(
            HarnessMessage.objects.filter(session_id=session_id).order_by("created_at")
        )

    @staticmethod
    def latest_assistant_completed_at_by_session(
        session_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, datetime]:
        """Return the latest completed assistant message timestamp per session."""
        if not session_ids:
            return {}
        from django.db.models import Max

        from .models import HarnessMessageRole

        rows = (
            HarnessMessage.objects.filter(
                session_id__in=session_ids,
                role=HarnessMessageRole.ASSISTANT,
                completed_at__isnull=False,
            )
            .values("session_id")
            .annotate(latest=Max("completed_at"))
        )
        return {row["session_id"]: row["latest"] for row in rows}

    @staticmethod
    def append_content(message: HarnessMessage, delta: str) -> HarnessMessage:
        """Append a text delta to an assistant message."""
        message.content = f"{message.content or ''}{delta or ''}"
        message.save(update_fields=["content"])
        return message

    @staticmethod
    def add_usage(
        message: HarnessMessage,
        *,
        cost: float = 0.0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> HarnessMessage:
        """Accumulate cost/tokens counters on a message."""
        tokens = dict(message.tokens or {})
        tokens["prompt"] = int(tokens.get("prompt", 0)) + prompt_tokens
        tokens["completion"] = int(tokens.get("completion", 0)) + (completion_tokens)
        tokens["total"] = int(tokens.get("total", 0)) + total_tokens
        message.tokens = tokens
        message.cost = float(message.cost or 0.0) + float(cost or 0.0)
        message.save(update_fields=["tokens", "cost"])
        return message

    @staticmethod
    def complete(
        message: HarnessMessage,
        *,
        finish: str = "",
        error: str = "",
    ) -> HarnessMessage:
        """Mark a message completed (finish reason or abort/error)."""
        message.finish = finish or message.finish
        message.error = error or ""
        message.completed_at = timezone.now()
        message.save(update_fields=["finish", "error", "completed_at"])
        return message


class HarnessPartRepository:
    """Data access for HarnessPart records."""

    model = HarnessPart

    @staticmethod
    def create(
        *,
        message_id: uuid.UUID,
        type: str,
        state: str = "pending",
        call_id: str = "",
        title: str = "",
        input: dict | None = None,
        output: str = "",
        meta: dict | None = None,
    ) -> HarnessPart:
        """Create a streamed part shell for an assistant message."""
        return HarnessPart.objects.create(
            message_id=message_id,
            type=type,
            state=state,
            call_id=call_id or "",
            title=title or "",
            input=dict(input or {}),
            output=output or "",
            meta=dict(meta or {}),
        )

    @staticmethod
    def list_for_session(session_id: uuid.UUID) -> list[HarnessPart]:
        """Return all parts of a session ordered by creation."""
        return list(
            HarnessPart.objects.filter(message__session_id=session_id).order_by(
                "created_at"
            )
        )

    @staticmethod
    def list_for_message(message_id: uuid.UUID) -> list[HarnessPart]:
        """Return all parts of one message ordered by creation."""
        return list(
            HarnessPart.objects.filter(message_id=message_id).order_by("created_at")
        )

    @staticmethod
    def append_output(part: HarnessPart, delta: str) -> HarnessPart:
        """Append a text/reasoning delta to a part's output."""
        part.output = f"{part.output or ''}{delta or ''}"
        part.save(update_fields=["output", "updated_at"])
        return part

    @staticmethod
    def mark_state(
        part: HarnessPart,
        state: str,
        *,
        title: str | None = None,
        output: str | None = None,
        meta: dict | None = None,
    ) -> HarnessPart:
        """Transition a part to *state* with optional field updates."""
        part.state = state
        fields = ["state", "updated_at"]
        if title is not None:
            part.title = title
            fields.append("title")
        if output is not None:
            part.output = output
            fields.append("output")
        if meta is not None:
            merged = dict(part.meta or {})
            merged.update(meta)
            part.meta = merged
            fields.append("meta")
        part.save(update_fields=fields)
        return part


class TodoRepositoryDjango:
    """Persistent ``TodoRepository`` (M3 interface) backed by ``Todo``.

    Drop-in replacement for the M3 ``InMemoryTodoRepository``: the only
    seam ``TodoWriteTool`` talks to. Once a run carries a real session
    UUID, ``HarnessService`` builds tools with this repository wired in
    (see ``services._tools_for_session``); the tool code itself is
    untouched.
    """

    def list(self, session_id: str):  # type: ignore[no-untyped-def]
        """Return the todo list for *session_id* (empty if none)."""
        from .tools.todos import TodoList

        try:
            session_uuid = uuid.UUID(str(session_id))
        except (TypeError, ValueError):
            return TodoList(session_id=str(session_id))
        rows = list(
            Todo.objects.filter(session_id=session_uuid).order_by("order", "created_at")
        )
        return TodoList(
            session_id=str(session_id),
            items=[_todo_row_to_item(row) for row in rows if row is not None],
        )

    def save(self, session_id: str, items: list) -> object:  # type: ignore[no-untyped-def]
        """Replace the todo list for *session_id* and return it."""
        try:
            session_uuid = uuid.UUID(str(session_id))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Todo save needs a real session UUID, got {session_id!r}"
            ) from exc
        Todo.objects.filter(session_id=session_uuid).delete()
        for order, item in enumerate(items):
            Todo.objects.create(
                session_id=session_uuid,
                content=item.content,
                status=item.status,
                priority=item.priority or "medium",
                order=order,
            )
        return self.list(str(session_id))


def _todo_row_to_item(row: Todo):  # type: ignore[no-untyped-def]
    """Map a ``Todo`` ORM row to the M3 ``TodoItem`` dataclass."""
    from .tools.todos import TodoItem

    return TodoItem(
        content=row.content,
        status=row.status,
        priority=row.priority or "medium",
        order=row.order,
    )


class TodoRepository:
    """Data access for ``Todo`` records (explicit CRUD for API/tests)."""

    model = Todo

    @staticmethod
    def list_for_session(session_id: uuid.UUID) -> list[Todo]:
        """Return all todos of a session in display order."""
        return list(
            Todo.objects.filter(session_id=session_id).order_by("order", "created_at")
        )


class QuestionRequestRepository:
    """Data access for ``QuestionRequest`` records."""

    model = QuestionRequest

    @staticmethod
    def create(
        *,
        organization_id: uuid.UUID,
        session_id: uuid.UUID,
        questions: list[dict[str, object]],
        workspace_id: uuid.UUID | None = None,
        message_id: uuid.UUID | None = None,
        call_id: str = "",
    ) -> QuestionRequest:
        """Persist a pending question request."""
        return QuestionRequest.objects.create(
            organization_id=organization_id,
            workspace_id=workspace_id,
            session_id=session_id,
            message_id=message_id,
            call_id=call_id or "",
            questions=list(questions or []),
        )

    @staticmethod
    def get_by_id(request_id: uuid.UUID) -> QuestionRequest | None:
        """Fetch a question request by primary key."""
        return QuestionRequest.objects.filter(id=request_id).first()

    @staticmethod
    def list_pending_for_session(session_id: uuid.UUID) -> list[QuestionRequest]:
        """Return pending question requests for *session_id*."""
        return list(
            QuestionRequest.objects.filter(
                session_id=session_id,
                status=QuestionRequestStatus.PENDING,
            ).order_by("created_at")
        )

    @staticmethod
    def list_pending_for_sessions(
        session_ids: list[uuid.UUID],
    ) -> list[QuestionRequest]:
        """Return pending question requests for any of *session_ids*."""
        if not session_ids:
            return []
        return list(
            QuestionRequest.objects.filter(
                session_id__in=session_ids,
                status=QuestionRequestStatus.PENDING,
            ).order_by("created_at")
        )

    @staticmethod
    def resolve(
        request: QuestionRequest,
        *,
        answers: list[object],
        status: str,
    ) -> QuestionRequest:
        """Mark *request* answered/rejected and store answers."""
        request.answers = list(answers or [])
        request.status = status
        request.resolved_at = timezone.now()
        request.save(
            update_fields=["answers", "status", "resolved_at"],
        )
        return request
