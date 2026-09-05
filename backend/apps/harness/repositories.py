"""
Repository layer for the harness app.

Encapsulates all database queries. Services never use the ORM directly.
"""

from __future__ import annotations

import uuid

from django.utils import timezone

from .models import (
    HarnessMessage,
    HarnessPart,
    HarnessSession,
    ProviderConfig,
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
        api_key_encrypted: str,
        base_url: str,
        default_model: str = "",
        small_model: str = "",
    ) -> ProviderConfig:
        """Create a provider config for an organization."""
        return ProviderConfig.objects.create(
            organization_id=organization_id,
            api_key_encrypted=api_key_encrypted,
            base_url=base_url,
            default_model=default_model,
            small_model=small_model,
        )

    @staticmethod
    def update(
        config: ProviderConfig,
        *,
        api_key_encrypted: str | None = None,
        base_url: str | None = None,
        default_model: str | None = None,
        small_model: str | None = None,
    ) -> ProviderConfig:
        """Update provider config fields."""
        update_fields = ["updated_at"]
        if api_key_encrypted is not None:
            config.api_key_encrypted = api_key_encrypted
            update_fields.append("api_key_encrypted")
        if base_url is not None:
            config.base_url = base_url
            update_fields.append("base_url")
        if default_model is not None:
            config.default_model = default_model
            update_fields.append("default_model")
        if small_model is not None:
            config.small_model = small_model
            update_fields.append("small_model")
        config.save(update_fields=update_fields)
        return config

    @staticmethod
    def delete_by_org(org_id: uuid.UUID) -> int:
        """Delete the provider config for an organization."""
        count, _ = ProviderConfig.objects.filter(organization_id=org_id).delete()
        return count


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
        parent_id: uuid.UUID | None = None,
    ) -> HarnessSession:
        """Create a harness session bound to a workspace."""
        return HarnessSession.objects.create(
            workspace_id=workspace_id,
            organization_id=organization_id,
            title=title or "",
            mode=mode,
            agent_name=agent_name,
            model=model or "",
            parent_id=parent_id,
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
    def mark_status(session: HarnessSession, status: str) -> HarnessSession:
        """Persist a session status change (busy|idle)."""
        session.status = status
        session.save(update_fields=["status", "updated_at"])
        return session

    @staticmethod
    def set_mode(session: HarnessSession, mode: str) -> HarnessSession:
        """Persist a session mode change (plan|build, idle only enforced by caller)."""
        session.mode = mode
        session.save(update_fields=["mode", "updated_at"])
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
        provider: str = "",
    ) -> HarnessMessage:
        """Create a user or assistant message shell."""
        return HarnessMessage.objects.create(
            session_id=session_id,
            role=role,
            content=content or "",
            model=model or "",
            provider=provider or "",
        )

    @staticmethod
    def list_for_session(session_id: uuid.UUID) -> list[HarnessMessage]:
        """Return all messages of a session ordered by creation."""
        return list(
            HarnessMessage.objects.filter(session_id=session_id).order_by("created_at")
        )

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
