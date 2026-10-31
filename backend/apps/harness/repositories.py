"""
Repository layer for the harness app.

Encapsulates all database queries. Services never use the ORM directly.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import nullcontext
from datetime import datetime

from django.db import IntegrityError, transaction
from django.db.models import (
    Case,
    Exists,
    F,
    Max,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    TextField,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Concat
from django.utils import timezone

from .models import (
    AgentConfig,
    ComputerUseConfig,
    HarnessMessage,
    HarnessMessageRole,
    HarnessPart,
    HarnessSession,
    HarnessSessionStatus,
    ProviderConfig,
    QuestionRequest,
    QuestionRequestStatus,
    RecentModel,
    SubagentConfig,
)
from .timeline import (
    TIMELINE_TEXT_PART_TYPES,
    append_part_display,
    build_part_display,
)

# Keep copy reads/allocations bounded and INSERTs below SQLite's parameter limit.
FORK_COPY_BATCH_SIZE = 50


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
        small_model: str = "",
        small_effort: str = "",
    ) -> ProviderConfig:
        """Create a provider config for an organization."""
        return ProviderConfig.objects.create(
            organization_id=organization_id,
            small_model=small_model,
            small_effort=small_effort,
        )

    @staticmethod
    def update(
        config: ProviderConfig,
        *,
        small_model: str | None = None,
        small_effort: str | None = None,
    ) -> ProviderConfig:
        """Update provider config fields."""
        update_fields = ["updated_at"]
        if small_model is not None:
            config.small_model = small_model
            update_fields.append("small_model")
        if small_effort is not None:
            config.small_effort = small_effort
            update_fields.append("small_effort")
        config.save(update_fields=update_fields)
        return config


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


class ComputerUseConfigRepository:
    """Data access for ComputerUseConfig records."""

    @staticmethod
    def get_by_org(org_id: uuid.UUID) -> ComputerUseConfig | None:
        """Fetch the computer-use config for an organization."""
        return ComputerUseConfig.objects.filter(organization_id=org_id).first()

    @staticmethod
    def create(*, organization_id: uuid.UUID, **fields: object) -> ComputerUseConfig:
        """Create the computer-use config row for an organization."""
        return ComputerUseConfig.objects.create(
            organization_id=organization_id,
            **fields,  # type: ignore[arg-type]
        )

    @staticmethod
    def update(config: ComputerUseConfig, **fields: object) -> ComputerUseConfig:
        """Update computer-use config fields (only provided fields)."""
        update_fields = ["updated_at"]
        for name, value in fields.items():
            setattr(config, name, value)
            update_fields.append(name)
        config.save(update_fields=update_fields)
        return config


class SubagentConfigRepository:
    """Data access for org-wide subagent depth configuration."""

    @staticmethod
    def get_by_org(org_id: uuid.UUID) -> SubagentConfig | None:
        """Fetch the subagent config for an organization."""
        return SubagentConfig.objects.filter(organization_id=org_id).first()

    @staticmethod
    def upsert(org_id: uuid.UUID, max_depth: int) -> SubagentConfig:
        """Create or update the organization's subagent depth."""
        row, _ = SubagentConfig.objects.update_or_create(
            organization_id=org_id, defaults={"max_depth": max_depth}
        )
        return row


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
        harness_id: str = "native",
        created_by_id: int | None = None,
        external_session_id: str = "",
        engine_state: dict | None = None,
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
            harness_id=(harness_id or "native").strip(),
            created_by_id=created_by_id,
            external_session_id=(external_session_id or "").strip(),
            engine_state=dict(engine_state or {}),
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
        harness_id: str = "native",
        created_by_id: int | None = None,
        external_session_id: str = "",
        engine_state: dict | None = None,
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
            harness_id=(harness_id or "native").strip(),
            created_by_id=created_by_id,
            external_session_id=(external_session_id or "").strip(),
            engine_state=dict(engine_state or {}),
            cost=0.0,
            tokens={},
            last_read_at=None,
            manual_unread_at=None,
            last_message_at=timezone.now(),
        )

    @staticmethod
    def reserve_run(session_id: uuid.UUID, *, scheduled: bool = False) -> bool:
        """Mark a root/child session busy; run under the workspace admission lock.

        A scheduled launch rejects any other busy root observed at that boundary.
        Manual roots do not reject an already-running chat, so a scheduled run
        never blocks a user's later chat.
        """
        session = HarnessSession.objects.get(id=session_id)
        if session.status == HarnessSessionStatus.BUSY:
            return False
        if (
            scheduled
            and HarnessSession.objects.filter(
                workspace_id=session.workspace_id,
                parent__isnull=True,
                status=HarnessSessionStatus.BUSY,
            )
            .exclude(id=session_id)
            .exists()
        ):
            return False
        return (
            HarnessSession.objects.filter(id=session_id)
            .exclude(status=HarnessSessionStatus.BUSY)
            .update(status=HarnessSessionStatus.BUSY)
            == 1
        )

    @staticmethod
    def delete_unstarted_root(
        session_id: uuid.UUID,
        *,
        workspace_id: uuid.UUID,
        organization_id: uuid.UUID,
        release: Callable[[], bool],
    ) -> bool:
        """Delete an empty, idle root session its creator releases.

        Run under the workspace admission lock so deletion serializes with run
        admission. ``release`` runs under that lock and returns False to keep
        the chat.
        """
        session = (
            HarnessSession.objects.select_for_update()
            .filter(
                pk=session_id,
                workspace_id=workspace_id,
                organization_id=organization_id,
                parent__isnull=True,
                status=HarnessSessionStatus.IDLE,
            )
            .first()
        )
        if session is None or session.messages.exists() or session.children.exists():
            return False
        if not release():
            return False
        session.delete()
        return True

    @staticmethod
    def get_by_id(session_id: uuid.UUID) -> HarnessSession | None:
        """Fetch a session by ID."""
        return HarnessSession.objects.filter(id=session_id).first()

    @staticmethod
    def get_depth(session_id: uuid.UUID) -> int:
        """Count persisted ancestors; reject broken or cross-scope ancestry."""
        session = HarnessSession.objects.get(id=session_id)
        depth = 0
        seen = {session.id}
        parent_id = session.parent_id
        while parent_id is not None:
            if parent_id in seen:
                raise ValueError("Cyclic subagent session ancestry")
            seen.add(parent_id)
            parent = (
                HarnessSession.objects.filter(
                    id=parent_id,
                    organization_id=session.organization_id,
                    workspace_id=session.workspace_id,
                )
                .only("parent_id")
                .first()
            )
            if parent is None:
                raise ValueError("Invalid subagent session ancestry")
            depth += 1
            parent_id = parent.parent_id
        return depth

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
        """Return *session_id* followed by descendant ids (breadth-first).

        Reads one tree level per query: every timeline load walks the tree,
        and wide subagent fan-outs must not cost one query per session.
        """
        ids: list[uuid.UUID] = [session_id]
        seen: set[uuid.UUID] = {session_id}
        level: list[uuid.UUID] = [session_id]
        while level:
            children = HarnessSession.objects.filter(parent_id__in=level).values_list(
                "id", flat=True
            )
            level = [child_id for child_id in children if child_id not in seen]
            seen.update(level)
            ids.extend(level)
        return ids

    @staticmethod
    def list_by_ids(session_ids: list[uuid.UUID]) -> list[HarnessSession]:
        """Return sessions for *session_ids* (order not guaranteed)."""
        if not session_ids:
            return []
        return list(HarnessSession.objects.filter(id__in=session_ids))

    @staticmethod
    def has_busy_for_workspace(workspace_id: uuid.UUID) -> bool:
        """Return True when any session in *workspace_id* is busy."""
        return bool(HarnessSessionRepository.busy_workspace_ids([workspace_id]))

    @staticmethod
    def busy_workspace_ids(workspace_ids: list[uuid.UUID]) -> set[uuid.UUID]:
        """Return requested workspaces with any busy root or child session."""
        if not workspace_ids:
            return set()
        return set(
            HarnessSession.objects.filter(
                workspace_id__in=workspace_ids, status=HarnessSessionStatus.BUSY
            )
            .order_by()
            .values_list("workspace_id", flat=True)
            .distinct()
        )

    @staticmethod
    def list_busy_computeruse_for_workspace(
        workspace_id: uuid.UUID,
    ) -> list[HarnessSession]:
        """Return busy specialist and root runs currently using Computer Use."""
        latest = (
            HarnessMessage.objects.filter(
                session_id=OuterRef("message__session_id"), role="assistant"
            )
            .order_by("-created_at")
            .values("id")[:1]
        )
        parts = HarnessPart.objects.filter(
            message__session_id=OuterRef("pk"),
            message_id=Subquery(latest),
            input__tool__startswith="mcp_computer-use_cua_repl_",
        )
        return list(
            HarnessSession.objects.filter(
                workspace_id=workspace_id,
                status=HarnessSessionStatus.BUSY,
            )
            .annotate(_uses_computer_use=Exists(parts))
            .filter(Q(agent_name="computeruse") | Q(_uses_computer_use=True))
            .order_by("created_at")
        )

    @staticmethod
    def list_for_workspaces(workspace_ids: list[uuid.UUID]) -> list[HarnessSession]:
        """Return root sessions across *workspace_ids* (newest first).

        Each row carries its owning workspace's name as ``workspace_name``.
        """
        if not workspace_ids:
            return []
        return list(
            HarnessSession.objects.filter(
                workspace_id__in=workspace_ids,
                parent__isnull=True,
            )
            .annotate(workspace_name=F("workspace__name"))
            .order_by("-last_message_at")
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
    def set_generated_title(
        session_id: uuid.UUID, expected_title: str, title: str
    ) -> bool:
        """Replace the prompt fallback atomically; a manual rename always wins."""
        return (
            HarnessSession.objects.filter(id=session_id, title=expected_title).update(
                title=(title or "").strip()[:255], updated_at=timezone.now()
            )
            == 1
        )

    @staticmethod
    def clear_summary(session_id: uuid.UUID) -> None:
        """Drop the status sentence once a new run of the session starts."""
        HarnessSession.objects.filter(id=session_id).exclude(summary="").update(
            summary=""
        )

    @staticmethod
    def set_summary_for_turn(
        session_id: uuid.UUID, assistant_message_id: uuid.UUID, summary: str
    ) -> bool:
        """Store the status sentence of a settled turn; False when outdated.

        Only applies while the session is idle and *assistant_message_id* is
        still its latest assistant message, so a slow generation cannot
        overwrite the state of a newer run.
        """
        latest_assistant_id = (
            HarnessMessage.objects.filter(
                session_id=OuterRef("pk"), role=HarnessMessageRole.ASSISTANT
            )
            .order_by("-position")
            .values("id")[:1]
        )
        return (
            HarnessSession.objects.filter(
                id=session_id, status=HarnessSessionStatus.IDLE
            )
            .annotate(latest_assistant_id=Subquery(latest_assistant_id))
            .filter(latest_assistant_id=assistant_message_id)
            .update(summary=(summary or "").strip()[:200])
            == 1
        )

    @staticmethod
    def set_skill_ids(session: HarnessSession, skill_ids: list[str]) -> HarnessSession:
        """Persist selected skill IDs for subsequent runs."""
        session.skill_ids = list(skill_ids or [])
        session.save(update_fields=["skill_ids", "updated_at"])
        return session

    @staticmethod
    def commit_message_edit(
        session: HarnessSession,
        *,
        suffix_ids: list[uuid.UUID],
        child_ids: list[uuid.UUID],
        model: str,
        reasoning_effort: str,
    ) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
        """Atomically replace an admitted edit's old timeline and run options."""
        from .engines.repositories import HarnessEngineRepository
        from .permissions.models import PermissionRequest

        with transaction.atomic():
            scope = Q(message_id__in=suffix_ids) | Q(message_id__isnull=True)
            permissions = PermissionRequest.objects.filter(
                scope, session_id=session.id, status="pending"
            )
            questions = QuestionRequest.objects.filter(
                scope, session_id=session.id, status="pending"
            )
            permission_ids = list(permissions.values_list("id", flat=True))
            question_ids = list(questions.values_list("id", flat=True))
            now = timezone.now()
            permissions.update(status="rejected", remember="once", resolved_at=now)
            questions.update(status="rejected", answers=[], resolved_at=now)
            HarnessSession.objects.filter(id__in=child_ids).delete()
            HarnessMessage.objects.filter(
                session_id=session.id, id__in=suffix_ids
            ).delete()
            updates = {
                "mode": session.mode,
                "agent_name": session.agent_name,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "skill_ids": list(session.skill_ids or []),
                "summary": "",
                "updated_at": now,
            }
            if session.harness_id != "native":
                HarnessEngineRepository.clear_transcript(session.id)
                state = {}
                if session.harness_id == "codex":
                    from .engines.codex.catalog import codex_provider

                    state = {
                        "provider_id": codex_provider(
                            session.model, session.engine_state
                        )
                    }
                updates.update(external_session_id="", engine_state=state)
            HarnessSession.objects.filter(id=session.id).update(**updates)
            HarnessMessageRepository.dismiss_prior_notices(session.id)
            return permission_ids, question_ids

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
        harness_id: str = "native",
        engine_meta: dict | None = None,
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
                        harness_id=(harness_id or "native").strip(),
                        engine_meta=dict(engine_meta or {}),
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
    def list_for_history(
        session_id: uuid.UUID,
        *,
        from_position: int = 0,
        from_message_id: uuid.UUID | None = None,
        exclude_message_ids: set[uuid.UUID],
    ) -> list[HarnessMessage]:
        """Read replay headers and user text from the retained message suffix.

        The caller chooses the fallback. Resolve only session-scoped, eligible
        anchors in the body query, without loading the discarded prefix.
        """
        query = HarnessMessage.objects.filter(session_id=session_id).exclude(
            id__in=exclude_message_ids
        )
        start: int | Coalesce = from_position
        if from_message_id is not None:
            anchor = query.filter(id=from_message_id).order_by().values("position")[:1]
            start = Coalesce(Subquery(anchor), Value(from_position))
        return list(
            query.filter(position__gte=start)
            .annotate(
                history_content=Case(
                    When(role=HarnessMessageRole.USER, then=F("content")),
                    default=Value(""),
                    output_field=TextField(),
                )
            )
            .only("id", "role")
            .order_by("position", "created_at", "id")
        )

    @staticmethod
    def list_timeline_for_session(session_id: uuid.UUID) -> list[HarnessMessage]:
        """Load timeline metadata and user text; assistant text lives in parts."""
        return list(
            HarnessMessage.objects.filter(session_id=session_id)
            .annotate(
                timeline_content=Case(
                    When(role=HarnessMessageRole.USER, then=F("content")),
                    default=Value(""),
                    output_field=TextField(),
                ),
            )
            .defer("content")
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
                "harness_id",
                "engine_meta",
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
    def get_required(message_id: uuid.UUID) -> HarnessMessage:
        """Fetch a fresh copy of a message that must exist.

        Raises:
            HarnessMessage.DoesNotExist: When the row is gone.
        """
        return HarnessMessage.objects.get(id=message_id)

    @staticmethod
    def refresh(message: HarnessMessage) -> HarnessMessage:
        """Reload *message* in place from the database."""
        message.refresh_from_db()
        return message

    @staticmethod
    def count_for_session(session_id: uuid.UUID, *, role: str) -> int:
        """Return how many *role* messages *session_id* holds."""
        return HarnessMessage.objects.filter(session_id=session_id, role=role).count()

    @staticmethod
    def get_latest_assistant(
        session_id: uuid.UUID, *, exclude_id: uuid.UUID
    ) -> HarnessMessage | None:
        """Return the newest assistant message of a session except *exclude_id*."""
        return (
            HarnessMessage.objects.filter(
                session_id=session_id,
                role=HarnessMessageRole.ASSISTANT,
            )
            .exclude(id=exclude_id)
            .order_by("-position", "-created_at", "-id")
            .first()
        )

    @staticmethod
    def list_for_summary(
        session_id: uuid.UUID, assistant_id: uuid.UUID
    ) -> list[HarnessMessage]:
        """Load summary fields through a session-scoped assistant anchor.

        Missing, foreign or user anchors yield no rows. Assistant text is read
        exclusively from the ordered parts.
        """
        anchor = (
            HarnessMessage.objects.filter(
                id=assistant_id,
                session_id=session_id,
                role=HarnessMessageRole.ASSISTANT,
            )
            .order_by()
            .values("position")[:1]
        )
        parts = (
            HarnessPart.objects.annotate(
                summary_text=Case(
                    When(type__in=TIMELINE_TEXT_PART_TYPES, then=F("output")),
                    default=Value(""),
                    output_field=TextField(),
                ),
            )
            .only(
                "message_id", "type", "state", "call_id", "title", "display", "position"
            )
            .order_by("position", "created_at", "id")
        )
        return list(
            HarnessMessage.objects.filter(
                session_id=session_id, position__lte=Subquery(anchor)
            )
            .annotate(
                summary_content=Case(
                    When(role=HarnessMessageRole.USER, then=F("content")),
                    default=Value(""),
                    output_field=TextField(),
                )
            )
            .only("id", "role")
            .prefetch_related(
                Prefetch("parts", queryset=parts, to_attr="summary_parts")
            )
            .order_by("position", "created_at", "id")
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
    def discard_edit_run(
        session_id: uuid.UUID,
        *,
        message_ids: list[uuid.UUID],
        last_message_at: datetime,
        updated_at: datetime,
    ) -> None:
        """Remove provisional edit shells when their run was not admitted."""
        with transaction.atomic():
            HarnessMessage.objects.filter(
                session_id=session_id, id__in=message_ids
            ).delete()
            HarnessSession.objects.filter(id=session_id).update(
                last_message_at=last_message_at, updated_at=updated_at
            )

    @staticmethod
    def copy_prefix(
        src_session_id: uuid.UUID,
        dst_session_id: uuid.UUID,
        cutoff_message_id: uuid.UUID | None,
    ) -> dict[uuid.UUID, uuid.UUID]:
        """Copy a message prefix to *dst_session_id*.

        Include the cutoff message; omit it to copy the full history.
        Unknown or foreign cutoffs are rejected.
        New UUIDs are generated; parts are copied in bounded batches with
        ``meta.tail_start_id`` remapped via the id map (dropped when outside
        the prefix) and subtask ``child_session_id`` cleared. Positions are
        reallocated sequentially to preserve chronological order. Destination
        writes are atomic and concurrent copies serialize on its session row.
        Returns the message ID map.
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
            if index is None:
                raise ValueError(f"Message '{cutoff_message_id}' not in session")
            ordered = ordered[: index + 1]
        if not ordered:
            return {}
        id_map = {src.id: uuid.uuid4() for src in ordered}
        for attempt in range(5):
            try:
                with transaction.atomic():
                    # Serialize copies without blocking appenders' FK checks.
                    HarnessSession.objects.select_for_update(no_key=True).only(
                        "id"
                    ).get(id=dst_session_id)
                    start = HarnessMessageRepository._next_message_position(
                        dst_session_id
                    )
                    last_user_at = None
                    for offset in range(0, len(ordered), FORK_COPY_BATCH_SIZE):
                        batch = [
                            HarnessMessage(
                                id=id_map[src.id],
                                session_id=dst_session_id,
                                position=start + offset + index,
                                role=src.role,
                                content=src.content or "",
                                model=src.model or "",
                                reasoning_effort=src.reasoning_effort or "",
                                provider=src.provider or "",
                                skill_ids=list(src.skill_ids or []),
                                harness_id=src.harness_id or "native",
                                engine_meta=dict(src.engine_meta or {}),
                                cost=float(src.cost or 0.0),
                                tokens=dict(src.tokens or {}),
                                finish=src.finish or "",
                                error=src.error or "",
                                notice_dismissed_at=src.notice_dismissed_at,
                                completed_at=src.completed_at,
                            )
                            for index, src in enumerate(
                                ordered[offset : offset + FORK_COPY_BATCH_SIZE]
                            )
                        ]
                        HarnessMessage.objects.bulk_create(
                            batch, batch_size=FORK_COPY_BATCH_SIZE
                        )
                        for message in batch:
                            if message.role == HarnessMessageRole.USER:
                                last_user_at = message.created_at
                    HarnessPartRepository.copy_for_messages(id_map)
                    if last_user_at is not None:
                        HarnessSessionRepository.touch_last_message_at(
                            dst_session_id, when=last_user_at
                        )
                return id_map
            except IntegrityError:
                # A concurrent ordinary message append can win a position race.
                # Retry the entire transaction, never a partially copied prefix.
                if attempt == 4:
                    raise

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
    def append_content(
        message: HarnessMessage,
        delta: str,
        *,
        text_part_id: uuid.UUID | str | None = None,
    ) -> HarnessMessage:
        """Append text, committing its stream part and message together when given.

        Text deltas do not change the part's display, so mirroring them needs
        only two writes and one commit, without loading either part column.
        """
        previous_content = message.content
        try:
            with transaction.atomic() if text_part_id is not None else nullcontext():
                # Match turn finalization's message -> part row-lock order.
                message.content = f"{previous_content or ''}{delta or ''}"
                message.save(update_fields=["content"])
                if text_part_id is not None:
                    HarnessPart.objects.filter(
                        id=text_part_id, message_id=message.id, type="text"
                    ).update(
                        output=Concat(
                            F("output"), Value(delta or ""), output_field=TextField()
                        ),
                        updated_at=timezone.now(),
                    )
        except Exception:
            if text_part_id is not None:
                message.content = previous_content
            raise
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
    def _prepare_meta(part_type: str, meta: dict | None) -> dict:
        part_meta = dict(meta or {})
        if part_type == "patch":
            part_meta.pop("old_content", None)
            part_meta.pop("new_content", None)
        return part_meta

    @staticmethod
    def copy_for_messages(message_id_map: dict[uuid.UUID, uuid.UUID]) -> None:
        """Copy ordered parts and regenerate displays for a message prefix.

        The caller owns the destination transaction and message allocation.
        Read and write batches bound temporary part payloads; no part positions
        need querying because each destination message is new.
        """
        source_ids = list(message_id_map)
        pending: list[HarnessPart] = []
        for offset in range(0, len(source_ids), FORK_COPY_BATCH_SIZE):
            parts = (
                HarnessPart.objects.filter(
                    message_id__in=source_ids[offset : offset + FORK_COPY_BATCH_SIZE]
                )
                .order_by(
                    "message__position",
                    "message__created_at",
                    "message_id",
                    "position",
                    "created_at",
                    "id",
                )
                .iterator(chunk_size=FORK_COPY_BATCH_SIZE)
            )
            previous_message_id = None
            position = 0
            for part in parts:
                if part.message_id != previous_message_id:
                    previous_message_id = part.message_id
                    position = 0
                meta = HarnessPartRepository._prepare_meta(part.type, part.meta)
                tail_raw = str(meta.get("tail_start_id", "") or "").strip()
                if tail_raw:
                    try:
                        tail_uuid = uuid.UUID(tail_raw)
                    except ValueError:
                        tail_uuid = None
                    if tail_uuid in message_id_map:
                        meta["tail_start_id"] = str(message_id_map[tail_uuid])
                    else:
                        meta.pop("tail_start_id", None)
                if part.type == "subtask":
                    meta["child_session_id"] = ""
                pending.append(
                    HarnessPart(
                        message_id=message_id_map[part.message_id],
                        position=position,
                        type=part.type,
                        state=part.state,
                        call_id=part.call_id or "",
                        title=part.title or "",
                        input=dict(part.input or {}),
                        output=part.output or "",
                        meta=meta,
                        display=build_part_display(
                            part_type=part.type,
                            title=part.title or "",
                            call_id=part.call_id or "",
                            input_data=part.input or {},
                            output=part.output or "",
                            meta_data=meta,
                        ),
                    )
                )
                position += 1
                if len(pending) == FORK_COPY_BATCH_SIZE:
                    HarnessPart.objects.bulk_create(
                        pending, batch_size=FORK_COPY_BATCH_SIZE
                    )
                    pending.clear()
        if pending:
            HarnessPart.objects.bulk_create(pending, batch_size=FORK_COPY_BATCH_SIZE)

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
                    part_meta = HarnessPartRepository._prepare_meta(type, meta)
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
    def list_timeline_for_session(session_id: uuid.UUID) -> list[HarnessPart]:
        """Fetch timeline rows without loading large tool/patch/agent/reasoning data."""
        rows = list(
            HarnessPart.objects.filter(message__session_id=session_id)
            .annotate(
                message_position=F("message__position"),
                timeline_output=Case(
                    When(type__in=TIMELINE_TEXT_PART_TYPES, then=F("output")),
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
    def get_by_id(part_id: uuid.UUID | str) -> HarnessPart | None:
        """Fetch a single part by ID."""
        return HarnessPart.objects.filter(id=part_id).first()

    @staticmethod
    def get_position(part_id: uuid.UUID | str) -> int | None:
        """Return the immutable timeline position of a part (None when missing)."""
        return (
            HarnessPart.objects.filter(id=part_id)
            .values_list("position", flat=True)
            .first()
        )

    @staticmethod
    def find_by_call_id(
        message_id: uuid.UUID,
        call_id: str,
        *,
        type: str | None = None,
        part_id: uuid.UUID | str | None = None,
    ) -> HarnessPart | None:
        """Return the part of *message_id* bound to *call_id*.

        *type* and *part_id* narrow the match to one part type or row.
        """
        query = HarnessPart.objects.filter(message_id=message_id, call_id=call_id)
        if type is not None:
            query = query.filter(type=type)
        if part_id is not None:
            query = query.filter(id=part_id)
        return query.first()

    @staticmethod
    def get_latest_for_message(
        message_id: uuid.UUID, *, type: str
    ) -> HarnessPart | None:
        """Return the newest *type* part of one message."""
        return (
            HarnessPart.objects.filter(message_id=message_id, type=type)
            .order_by("-position", "-created_at", "-id")
            .first()
        )

    @staticmethod
    def get_latest_completed_compaction(
        session_id: uuid.UUID,
        *,
        exclude_message_ids: set[uuid.UUID],
    ) -> HarnessPart | None:
        """Read the last checkpoint without loading unrelated history payloads."""
        return (
            HarnessPart.objects.filter(
                message__session_id=session_id,
                message__role=HarnessMessageRole.ASSISTANT,
                type="compaction",
                state="completed",
            )
            .exclude(message_id__in=exclude_message_ids)
            .annotate(message_position=F("message__position"))
            .only("message_id", "output", "meta")
            .order_by(
                "-message__position",
                "-message__created_at",
                "-message__id",
                "-position",
                "-created_at",
                "-id",
            )
            .first()
        )

    @staticmethod
    def list_for_message(message_id: uuid.UUID) -> list[HarnessPart]:
        """Return all parts of one message in timeline order."""
        return list(
            HarnessPart.objects.filter(message_id=message_id).order_by(
                "position", "created_at", "id"
            )
        )

    @staticmethod
    def list_for_messages(message_ids: list[uuid.UUID]) -> list[HarnessPart]:
        """Return all parts of any of *message_ids*."""
        return list(HarnessPart.objects.filter(message_id__in=message_ids))

    @staticmethod
    def history_parts_by_message(
        message_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, list[HarnessPart]]:
        """Read ordered replay parts, including empty groups, for a message batch."""
        grouped: dict[uuid.UUID, list[HarnessPart]] = {
            message_id: [] for message_id in message_ids
        }
        parts = (
            HarnessPart.objects.filter(
                message_id__in=message_ids, type__in=("text", "steer", "tool")
            )
            .only(
                "message_id",
                "type",
                "state",
                "call_id",
                "title",
                "input",
                "output",
                "meta",
            )
            .order_by("message_id", "position", "created_at", "id")
        )
        for part in parts:
            grouped[part.message_id].append(part)
        return grouped

    @staticmethod
    def list_open_for_message(
        message_id: uuid.UUID, *, types: tuple[str, ...] | None = None
    ) -> list[HarnessPart]:
        """Return pending/running parts of one message, optionally by type."""
        query = HarnessPart.objects.filter(
            message_id=message_id, state__in=("pending", "running")
        )
        if types is not None:
            query = query.filter(type__in=types)
        return list(query)

    @staticmethod
    def rebind_call_id(
        part: HarnessPart, call_id: str, *, meta: dict | None = None
    ) -> HarnessPart:
        """Bind *part* to the engine's real *call_id* (optionally new ``meta``)."""
        part.call_id = call_id
        fields = ["call_id", "updated_at"]
        if meta is not None:
            part.meta = meta
            fields = ["call_id", "meta", "updated_at"]
        part.save(update_fields=fields)
        return part

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
        next_display = append_part_display(display, part_type=part_type, delta=delta)
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
