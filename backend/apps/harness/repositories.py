"""
Repository layer for the harness app.

Encapsulates all database queries. Services never use the ORM directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from django.db import IntegrityError, transaction
from django.db.models import Case, Exists, F, Max, OuterRef, TextField, Value, When
from django.db.utils import NotSupportedError
from django.utils import timezone

from .models import (
    AgentConfig,
    AgentSConfig,
    HarnessMessage,
    HarnessMessageRole,
    HarnessPart,
    HarnessSession,
    HarnessSessionStatus,
    ProviderConfig,
    ProviderConnection,
    QuestionRequest,
    QuestionRequestStatus,
    RecentModel,
    Todo,
)
from .timeline import append_part_display, build_part_display


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


class AgentConfigRepository:
    """Data access for AgentConfig records."""

    @staticmethod
    def get_by_org_agent(org_id: uuid.UUID, agent: str) -> AgentConfig | None:
        """Fetch one agent config for an organization."""
        return AgentConfig.objects.filter(organization_id=org_id, agent=agent).first()

    @staticmethod
    def list_by_org(org_id: uuid.UUID) -> list[AgentConfig]:
        """List all agent configs for an organization."""
        return list(
            AgentConfig.objects.filter(organization_id=org_id).order_by("agent")
        )

    @staticmethod
    def upsert(
        org,
        agent: str,
        *,
        model: str = "",
        effort: str = "",
        inherit_model: bool = False,
        effort_strategy: str = "fixed",
    ) -> AgentConfig:
        """Create or update the agent config for an org+agent."""
        org_id = getattr(org, "id", org)
        config, _ = AgentConfig.objects.update_or_create(
            organization_id=org_id,
            agent=agent,
            defaults={
                "model": model,
                "effort": effort,
                "inherit_model": inherit_model,
                "effort_strategy": effort_strategy,
            },
        )
        return config

    @staticmethod
    def delete_by_org_agent(org_id: uuid.UUID, agent: str) -> bool:
        """Delete one agent config; return whether a row was removed."""
        deleted, _ = AgentConfig.objects.filter(
            organization_id=org_id, agent=agent
        ).delete()
        return deleted > 0


class AgentSConfigRepository:
    """Data access for AgentSConfig records."""

    @staticmethod
    def get_by_org(org_id: uuid.UUID) -> AgentSConfig | None:
        """Fetch the Agent-S config for an organization."""
        return AgentSConfig.objects.filter(organization_id=org_id).first()

    @staticmethod
    def create(*, organization_id: uuid.UUID, **fields: object) -> AgentSConfig:
        """Create the Agent-S config row for an organization."""
        return AgentSConfig.objects.create(
            organization_id=organization_id, **fields  # type: ignore[arg-type]
        )

    @staticmethod
    def update(config: AgentSConfig, **fields: object) -> AgentSConfig:
        """Update Agent-S config fields (only provided fields)."""
        update_fields = ["updated_at"]
        for name, value in fields.items():
            setattr(config, name, value)
            update_fields.append(name)
        config.save(update_fields=update_fields)
        return config


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


class RecentModelRepository:
    """Data access for per-user recent composer models (LRU, org-scoped)."""

    #: Server-side cap; the UI shows the first 6.
    MAX_ENTRIES = 10

    @staticmethod
    def list_by_user(org_id: uuid.UUID, user_id: object) -> list[RecentModel]:
        """List recent models for an org user, newest first."""
        return list(
            RecentModel.objects.filter(
                organization_id=org_id,
                user_id=user_id,
            ).order_by("-last_used_at", "-created_at")[
                : RecentModelRepository.MAX_ENTRIES
            ]
        )

    @staticmethod
    def record_usage(
        org_id: uuid.UUID,
        user,
        *,
        model: str,
        effort: str = "",
    ) -> RecentModel | None:
        """Upsert one recent-model row (LRU); ignore blank model ids.

        Oldest rows beyond MAX_ENTRIES are pruned so the table stays small.
        """
        model_id = (model or "").strip()
        if not model_id:
            return None
        effort_token = (effort or "").strip().lower()
        org_pk = getattr(org_id, "id", org_id)
        row, _ = RecentModel.objects.update_or_create(
            organization_id=org_pk,
            user_id=getattr(user, "id", user),
            model=model_id[:255],
            defaults={"effort": effort_token[:50]},
        )
        # Bump last_used_at explicitly: update_or_create only touches `effort`.
        RecentModel.objects.filter(pk=row.pk).update(last_used_at=timezone.now())
        row.refresh_from_db()
        # Prune overflow (keep newest MAX_ENTRIES).
        keep_ids = list(
            RecentModel.objects.filter(
                organization_id=org_pk,
                user_id=getattr(user, "id", user),
            )
            .order_by("-last_used_at", "-created_at")
            .values_list("id", flat=True)[: RecentModelRepository.MAX_ENTRIES]
        )
        RecentModel.objects.filter(
            organization_id=org_pk,
            user_id=getattr(user, "id", user),
        ).exclude(id__in=keep_ids).delete()
        return row


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
            last_message_at=timezone.now(),
        )

    @staticmethod
    def create_fork(
        *,
        workspace_id: uuid.UUID,
        organization_id: uuid.UUID,
        title: str = "",
        mode: str = "build",
        agent_name: str = "build",
        model: str = "",
        reasoning_effort: str = "",
        skill_ids: list[str] | None = None,
    ) -> HarnessSession:
        """Create a root fork session (parent always None, fresh usage)."""
        return HarnessSession.objects.create(
            workspace_id=workspace_id,
            organization_id=organization_id,
            title=title or "",
            mode=mode,
            agent_name=agent_name,
            model=model or "",
            reasoning_effort=reasoning_effort or "",
            parent_id=None,
            skill_ids=list(skill_ids or []),
            cost=0.0,
            tokens={},
            last_read_at=None,
            manual_unread_at=None,
            last_message_at=timezone.now(),
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
            ).order_by("-last_message_at")
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
    def touch_last_message_at(
        session_id: uuid.UUID,
        when: datetime | None = None,
    ) -> None:
        """Record that a completed user or assistant message landed."""
        HarnessSession.objects.filter(id=session_id).update(
            last_message_at=when or timezone.now()
        )

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
    def _next_message_position(session_id: uuid.UUID) -> int:
        """Return max(position)+1 for *session_id* (0 when empty)."""
        row = HarnessMessage.objects.filter(session_id=session_id).aggregate(
            top=Max("position")
        )
        top = row.get("top")
        return int(top) + 1 if top is not None else 0

    @staticmethod
    def create(
        *,
        session_id: uuid.UUID,
        role: str,
        content: str = "",
        model: str = "",
        reasoning_effort: str = "",
        provider: str = "",
        skill_ids: list[str] | None = None,
    ) -> HarnessMessage:
        """Create a user or assistant message shell.

        ``position`` is allocated as ``max(position)+1`` inside a
        transaction; on unique-constraint races the allocation retries
        so concurrent writers never collide (SQLite-safe fallback).
        """
        attempts = 0
        while True:
            attempts += 1
            try:
                with transaction.atomic():
                    position = HarnessMessageRepository._next_message_position(
                        session_id
                    )
                    message = HarnessMessage.objects.create(
                        session_id=session_id,
                        role=role,
                        content=content or "",
                        model=model or "",
                        reasoning_effort=reasoning_effort or "",
                        provider=provider or "",
                        skill_ids=list(skill_ids or []),
                        position=position,
                    )
                break
            except IntegrityError:
                if attempts >= 5:
                    raise
                continue
        if role == HarnessMessageRole.USER:
            HarnessSessionRepository.touch_last_message_at(
                session_id, when=message.created_at
            )
        return message

    @staticmethod
    def list_for_session(session_id: uuid.UUID) -> list[HarnessMessage]:
        """Return all messages of a session in timeline order."""
        return list(
            HarnessMessage.objects.filter(session_id=session_id).order_by(
                "position", "created_at", "id"
            )
        )

    @staticmethod
    def list_timeline_for_session(session_id: uuid.UUID) -> list[HarnessMessage]:
        """Load timeline metadata and only content lacking text-part backing."""
        has_text = Exists(
            HarnessPart.objects.filter(message_id=OuterRef("pk"), type="text")
        )
        try:
            queryset = HarnessMessage.objects.filter(session_id=session_id).annotate(
                has_text_parts=has_text,
                timeline_content=Case(
                    When(
                        role=HarnessMessageRole.ASSISTANT,
                        has_text_parts=True,
                        then=Value(""),
                    ),
                    default=F("content"),
                    output_field=TextField(),
                ),
            )
        except NotSupportedError:
            # Test repositories such as SQLite may lack correlated subqueries
            # for the project backend. Keep a one-query safe fallback there.
            queryset = HarnessMessage.objects.filter(session_id=session_id).annotate(
                timeline_content=F("content")
            )
        return list(
            queryset.defer("content")
            .only(
                "id",
                "session_id",
                "role",
                "model",
                "reasoning_effort",
                "cost",
                "tokens",
                "finish",
                "error",
                "skill_ids",
                "notice_dismissed_at",
                "position",
                "created_at",
                "completed_at",
            )
            .order_by("position", "created_at", "id")
        )

    @staticmethod
    def get_by_id(message_id: uuid.UUID) -> HarnessMessage | None:
        """Fetch a single message by ID."""
        return HarnessMessage.objects.filter(id=message_id).first()

    @staticmethod
    def list_ids_for_session(session_id: uuid.UUID) -> list[uuid.UUID]:
        """Return message IDs of a session in timeline order."""
        return list(
            HarnessMessage.objects.filter(session_id=session_id)
            .order_by("position", "created_at", "id")
            .values_list("id", flat=True)
        )

    @staticmethod
    def delete_from(
        session_id: uuid.UUID, from_message_id: uuid.UUID
    ) -> list[uuid.UUID]:
        """Delete messages from *from_message_id* onward (inclusive).

        Ordering is ``position`` (ties broken by ``created_at``, ``id``).
        Related parts are removed via CASCADE. Returns deleted message IDs.
        """
        ordered = list(
            HarnessMessage.objects.filter(session_id=session_id).order_by(
                "position", "created_at", "id"
            )
        )
        index = next(
            (pos for pos, row in enumerate(ordered) if row.id == from_message_id),
            None,
        )
        if index is None:
            raise ValueError(f"Message '{from_message_id}' not in session")
        suffix = ordered[index:]
        deleted_ids = [row.id for row in suffix]
        if deleted_ids:
            HarnessMessage.objects.filter(
                session_id=session_id, id__in=deleted_ids
            ).delete()
        return deleted_ids

    @staticmethod
    def copy_prefix(
        src_session_id: uuid.UUID,
        dst_session_id: uuid.UUID,
        cutoff_message_id: uuid.UUID | None,
    ) -> dict[uuid.UUID, uuid.UUID]:
        """Copy message prefix to *dst_session_id* (cutoff exclusive).

        When *cutoff_message_id* is None or not found, the whole
        history is copied. New UUIDs are generated; parts are copied
        per message with ``meta.tail_start_id`` remapped via the id
        map (dropped when outside the prefix) and subtask
        ``child_session_id`` cleared. Positions are reallocated
        sequentially via repo ``create`` so the fork keeps chronological
        order. Returns the message id map.
        """
        ordered = list(
            HarnessMessage.objects.filter(session_id=src_session_id).order_by(
                "position", "created_at", "id"
            )
        )
        if cutoff_message_id is not None:
            index = next(
                (pos for pos, row in enumerate(ordered) if row.id == cutoff_message_id),
                None,
            )
            if index is not None:
                ordered = ordered[:index]
        id_map: dict[uuid.UUID, uuid.UUID] = {}
        # Allocate message positions sequentially in chronological order.
        snapshots: list[HarnessMessage] = list(ordered)
        for src in snapshots:
            dst = HarnessMessageRepository.create(
                session_id=dst_session_id,
                role=src.role,
                content=src.content or "",
                model=src.model or "",
                reasoning_effort=src.reasoning_effort or "",
                provider=src.provider or "",
                skill_ids=list(getattr(src, "skill_ids", None) or []),
            )
            HarnessMessage.objects.filter(id=dst.id).update(
                cost=float(src.cost or 0.0),
                tokens=dict(src.tokens or {}),
                finish=src.finish or "",
                error=src.error or "",
                notice_dismissed_at=src.notice_dismissed_at,
                completed_at=src.completed_at,
            )
            dst.refresh_from_db()
            id_map[src.id] = dst.id
        for src in snapshots:
            dst_id = id_map[src.id]
            parts = list(
                HarnessPart.objects.filter(message_id=src.id).order_by(
                    "position", "created_at", "id"
                )
            )
            for part in parts:
                meta = dict(part.meta or {})
                tail_raw = str(meta.get("tail_start_id", "") or "").strip()
                if tail_raw:
                    try:
                        tail_uuid = uuid.UUID(tail_raw)
                    except ValueError:
                        tail_uuid = None
                    if tail_uuid is not None and tail_uuid in id_map:
                        meta["tail_start_id"] = str(id_map[tail_uuid])
                    else:
                        meta.pop("tail_start_id", None)
                if part.type == "subtask":
                    meta["child_session_id"] = ""
                HarnessPartRepository.create(
                    message_id=dst_id,
                    type=part.type,
                    state=part.state,
                    call_id=part.call_id or "",
                    title=part.title or "",
                    input=dict(part.input or {}),
                    output=part.output or "",
                    meta=meta,
                )
        return id_map

    @staticmethod
    def latest_assistant_completed_at_by_session(
        session_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, datetime]:
        """Return the latest completed assistant message timestamp per session."""
        if not session_ids:
            return {}
        from django.db.models import Max

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
        HarnessSessionRepository.touch_last_message_at(
            message.session_id, when=message.completed_at
        )
        return message

    @staticmethod
    def dismiss_notice(message: HarnessMessage) -> HarnessMessage:
        """Mark the stopped/failed notice of *message* as dismissed."""
        message.notice_dismissed_at = timezone.now()
        message.save(update_fields=["notice_dismissed_at"])
        return message

    @staticmethod
    def dismiss_prior_notices(
        session_id: uuid.UUID,
        *,
        exclude_ids: list[uuid.UUID] | None = None,
    ) -> int:
        """Dismiss stopped/failed notices of earlier messages in *session_id*.

        Targets assistant messages with an error payload
        (``error`` set or ``finish`` in ``aborted``/``error``) that are
        not dismissed yet. Returns the number of updated rows.
        """
        from django.db.models import Q

        queryset = HarnessMessage.objects.filter(
            Q(session_id=session_id)
            & Q(role=HarnessMessageRole.ASSISTANT)
            & Q(notice_dismissed_at__isnull=True)
            & (~Q(error="") | Q(finish__in=("aborted", "error")))
        )
        if exclude_ids:
            queryset = queryset.exclude(id__in=list(exclude_ids))
        return queryset.update(notice_dismissed_at=timezone.now())


class HarnessPartRepository:
    """Data access for HarnessPart records."""

    model = HarnessPart

    @staticmethod
    def _next_part_position(message_id: uuid.UUID) -> int:
        """Return max(position)+1 for *message_id* (0 when empty)."""
        row = HarnessPart.objects.filter(message_id=message_id).aggregate(
            top=Max("position")
        )
        top = row.get("top")
        return int(top) + 1 if top is not None else 0

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
        """Create a streamed part shell for an assistant message.

        ``position`` is allocated as ``max(position)+1`` inside a
        transaction; on unique-constraint races the allocation retries
        so concurrent writers never collide (SQLite-safe fallback).
        """
        attempts = 0
        while True:
            attempts += 1
            try:
                with transaction.atomic():
                    position = HarnessPartRepository._next_part_position(message_id)
                    part_meta = dict(meta or {})
                    if type == "patch":
                        part_meta.pop("old_content", None)
                        part_meta.pop("new_content", None)
                    return HarnessPart.objects.create(
                        message_id=message_id,
                        type=type,
                        state=state,
                        call_id=call_id or "",
                        title=title or "",
                        input=dict(input or {}),
                        output=output or "",
                        meta=part_meta,
                        display=build_part_display(
                            part_type=type,
                            title=title or "",
                            call_id=call_id or "",
                            input_data=input or {},
                            output=output or "",
                            meta_data=part_meta,
                        ),
                        position=position,
                    )
            except IntegrityError:
                if attempts >= 5:
                    raise
                continue

    @staticmethod
    def list_for_session(session_id: uuid.UUID) -> list[HarnessPart]:
        """Return all full parts of a session in timeline order (legacy path)."""
        return list(
            HarnessPart.objects.filter(message__session_id=session_id).order_by(
                "message__position",
                "message__created_at",
                "message__id",
                "position",
                "created_at",
                "id",
            )
        )

    @staticmethod
    def list_timeline_for_session(session_id: uuid.UUID) -> list[HarnessPart]:
        """Fetch timeline rows without loading large tool/patch/agent/reasoning data."""
        rows = list(
            HarnessPart.objects.filter(message__session_id=session_id)
            .annotate(
                message_position=F("message__position"),
                timeline_output=Case(
                    When(type="text", then=F("output")),
                    default=Value(""),
                    output_field=TextField(),
                ),
            )
            .only(
                "id",
                "message_id",
                "type",
                "state",
                "call_id",
                "title",
                "position",
                "created_at",
                "display",
            )
            .order_by(
                "message__position",
                "message__created_at",
                "message__id",
                "position",
                "created_at",
                "id",
            )
        )
        if not rows:
            return rows

        # Large tool/patch/agent/reasoning bodies are deferred; text output is
        # projected conditionally in the same query for ordinary rendering.
        return rows

    @staticmethod
    def get_for_session(
        session_id: uuid.UUID, part_id: uuid.UUID
    ) -> HarnessPart | None:
        """Fetch a full part only when it belongs to the requested session."""
        part = HarnessPart.objects.filter(
            id=part_id, message__session_id=session_id
        ).first()
        if part is not None:
            part.message_position = (
                HarnessMessage.objects.filter(id=part.message_id)
                .values_list("position", flat=True)
                .first()
                or 0
            )
        return part

    @staticmethod
    def list_for_message(message_id: uuid.UUID) -> list[HarnessPart]:
        """Return all parts of one message in timeline order."""
        return list(
            HarnessPart.objects.filter(message_id=message_id).order_by(
                "position", "created_at", "id"
            )
        )

    @staticmethod
    def set_input(part: HarnessPart, input_data: dict) -> HarnessPart:
        """Replace a part input payload and refresh its lightweight display."""
        part.input = dict(input_data or {})
        part.display = build_part_display(
            part_type=part.type,
            title=part.title,
            call_id=part.call_id,
            input_data=part.input,
            output=part.output,
            meta_data=part.meta,
        )
        part.save(update_fields=["input", "display", "updated_at"])
        return part

    @staticmethod
    def append_output_by_id(part_id: uuid.UUID, delta: str) -> None:
        """Append a stream delta without loading the growing output column."""
        from django.db.models.functions import Concat

        delta = delta or ""
        if not delta:
            return
        row = (
            HarnessPart.objects.filter(id=part_id)
            .values_list("display", "type")
            .first()
        )
        if row is None:
            return
        display, part_type = row
        next_display = append_part_display(
            display, part_type=part_type, delta=delta
        )
        HarnessPart.objects.filter(id=part_id).update(
            output=Concat(F("output"), Value(delta), output_field=TextField()),
            display=next_display,
            updated_at=timezone.now(),
        )

    @staticmethod
    def append_output(part: HarnessPart, delta: str) -> HarnessPart:
        """Append a text/reasoning delta and refresh the lightweight display."""
        part.output = f"{part.output or ''}{delta or ''}"
        # Reasoning summaries depend only on the final line. Advance that
        # bounded projection from the delta instead of scanning the full body
        # for every token (stream persistence itself remains append-only).
        part.display = append_part_display(
            part.display, part_type=part.type, delta=delta or ""
        )
        part.save(update_fields=["output", "display", "updated_at"])
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
            if part.type == "patch":
                merged.pop("old_content", None)
                merged.pop("new_content", None)
            part.meta = merged
            fields.append("meta")
        if part.type == "patch":
            part.meta = dict(part.meta or {})
            part.meta.pop("old_content", None)
            part.meta.pop("new_content", None)
            if "meta" not in fields:
                fields.append("meta")
        part.display = build_part_display(
            part_type=part.type,
            title=part.title,
            call_id=part.call_id,
            input_data=part.input,
            output=part.output,
            meta_data=part.meta,
        )
        fields.append("display")
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
