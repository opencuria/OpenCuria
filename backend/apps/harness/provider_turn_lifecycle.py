"""Provider-account scope, revocation monitoring and answer holders."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anyio
import structlog
from asgiref.sync import sync_to_async

from .interaction_service import InteractionService
from .models import (
    HarnessSession,
)
from .provider_account_events import (
    unregister_revocation_listener,
)
from .provider_turns import ProviderTurnService
from .run_state import RunRegistry

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ProviderTurnLifecyclePorts:
    """Explicit domain collaborators; no facade or sibling private state."""

    cancel_owned_task: Callable[..., Any]
    descendant_keys: Callable[..., Any]
    owned_run_task: Callable[..., Any]
    provider_turn_roots: dict[str, dict[str, Any]]
    provider_turn_service: ProviderTurnService
    interactions: InteractionService
    run_registry: RunRegistry


class ProviderTurnLifecycle:
    """ProviderTurnLifecycle domain service."""

    def __init__(self, ports: ProviderTurnLifecyclePorts) -> None:
        self.ports = ports

    def _background_contexts(self) -> tuple[dict[str, Any], ...]:
        """Include detached metadata roots and still-admitting child contexts."""
        contexts = list(self.ports.provider_turn_roots.values())
        contexts.extend(
            run.context
            for run in self.ports.run_registry.records.values()
            if run.context is not None
        )
        return tuple({id(context): context for context in contexts}.values())

    def cancel_background_work(self) -> tuple[dict[str, Any], ...]:
        """Stop metadata/monitors before core teardown can publish new results."""
        contexts = self._background_contexts()
        for context in contexts:
            tasks = [context.get("provider_monitor_task")]
            tasks.extend(context.get("provider_followup_tasks", {}).values())
            for task in tasks:
                if task is not None and not task.done() and not task.cancelling():
                    task.cancel()
        return contexts

    async def shutdown(self, contexts: tuple[dict[str, Any], ...]) -> None:
        """Join provider tasks and release each named holder after core teardown."""
        current = self.cancel_background_work()
        contexts = tuple({id(item): item for item in (*contexts, *current)}.values())
        followers = [
            (context, token, task)
            for context in contexts
            for token, task in tuple(context.get("provider_followup_tasks", {}).items())
        ]
        tasks = {task for _, _, task in followers}
        tasks.update(
            context["provider_monitor_task"]
            for context in contexts
            if context.get("provider_monitor_task") is not None
        )
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Registration precedes monitor task entry. Even a never-started monitor
        # has a subscription that this process must explicitly relinquish.
        for context in contexts:
            watched = context.get("provider_revocation_accounts")
            event = context.get("provider_revocation_event")
            if watched and event is not None:
                unregister_revocation_listener(watched, event=event)
        # Callbacks may release the same holder concurrently. finish_once is
        # intentionally the single idempotent persistence boundary for both.
        for context, holder, _ in followers:
            turn_id = context.get("provider_turn_id")
            if turn_id:
                await self._finish_provider_hold(turn_id, holder)
        for context in contexts:
            # An external process whose cleanup is unconfirmed still owns its
            # durable answer snapshot. Recovery, not process shutdown, releases
            # that holder after the runner confirms the lease is closed.
            if context.get("cleanup_confirmed", True):
                await self._finish_provider_turn(context)
            turn_id = context.get("provider_turn_id")
            if not turn_id:
                continue
            state = await sync_to_async(self.ports.provider_turn_service.get)(turn_id)
            if state is not None and state.active and not state.holds:
                await sync_to_async(
                    self.ports.provider_turn_service.clear_monitor_required
                )(turn_id)
            await self._maybe_finish_provider_monitor(context)

    def _provider_turn_context(self, session: HarnessSession) -> dict[str, Any]:
        """Find the snapshot context at this session or its live ancestor."""
        key = str(session.id)
        seen: set[str] = set()
        parent = str(session.parent_id) if session.parent_id else None
        while key and key not in seen:
            seen.add(key)
            context = self.ports.run_registry.contexts.get(key, {})
            if context.get("provider_turn_id"):
                return context
            parent = self.ports.run_registry.parents.get(key, parent)
            if parent is None:
                return {}
            key, parent = parent, self.ports.run_registry.parents.get(parent)
        return {}

    async def _register_provider_use(
        self, turn_id: uuid.UUID | str | None, account_id: uuid.UUID | str | None
    ) -> None:
        """Fence a provider request just before its async network operation."""
        if turn_id is not None and account_id is not None:
            await sync_to_async(self.ports.provider_turn_service.register_use)(
                turn_id, account_id
            )

    async def _provider_turn_revocation_message(self, session: HarnessSession) -> str:
        """Read durable revocation only at run control/terminal boundaries."""
        with anyio.CancelScope(shield=True):
            context = self._provider_turn_context(session)
            if context.get("provider_revocation_message"):
                return str(context["provider_revocation_message"])
            turn_id = context.get("provider_turn_id")
            if turn_id and await sync_to_async(
                self.ports.provider_turn_service.cancellation_reason
            )(turn_id):
                return (
                    "This response was stopped because its selected provider account "
                    "was removed or is no longer shared."
                )
            return ""

    async def _finish_provider_turn(self, context: dict[str, Any]) -> None:
        """Idempotently release this run's named answer holder."""
        turn_id = context.get("provider_turn_id")
        holder = context.get("provider_turn_holder")
        if turn_id and context.get("provider_revocation_message"):
            root = self.ports.provider_turn_roots.get(str(turn_id), context)
            followers = tuple(root.get("provider_followup_tasks", {}).items())
            for _, task in followers:
                if not task.done() and task is not asyncio.current_task():
                    task.cancel()
            for token, task in followers:
                if task is asyncio.current_task():
                    continue
                try:
                    await asyncio.shield(task)
                except (asyncio.CancelledError, Exception):
                    pass
                await sync_to_async(self.ports.provider_turn_service.finish_once)(
                    turn_id, token
                )
        if turn_id and holder and not context.get("provider_turn_finished"):
            context["provider_turn_finished"] = True
            await sync_to_async(self.ports.provider_turn_service.finish_once)(
                turn_id, holder
            )

    async def _finish_provider_hold(
        self, turn_id: uuid.UUID | str, holder: str
    ) -> None:
        """Idempotently release a named title/summary holder."""
        await sync_to_async(self.ports.provider_turn_service.finish_once)(
            turn_id, holder
        )
        root = self.ports.provider_turn_roots.get(str(turn_id))
        if root is not None:
            root.setdefault("provider_followup_tasks", {}).pop(holder, None)
            await self._maybe_finish_provider_monitor(root)

    async def _maybe_finish_provider_monitor(self, root: dict[str, Any]) -> None:
        """Stop the root monitor only after the durable answer has no holders."""
        turn_id = root.get("provider_turn_id")
        if not turn_id:
            return
        state = await sync_to_async(self.ports.provider_turn_service.get)(turn_id)
        if (
            state is None
            or not state.active
            or (not state.monitor_required and not state.holds)
        ):
            monitor = root.get("provider_monitor_task")
            if (
                monitor is not None
                and not monitor.done()
                and monitor is not asyncio.current_task()
            ):
                monitor.cancel()
            watched = root.get("provider_revocation_accounts")
            event = root.get("provider_revocation_event")
            if watched and event is not None:
                unregister_revocation_listener(watched, event=event)
            self.ports.provider_turn_roots.pop(str(turn_id), None)

    def _watch_provider_followup(
        self, context: dict[str, Any], task: asyncio.Task, holder: str
    ) -> None:
        """Keep follow-up tasks visible to revocation after root cleanup."""
        tasks = context.setdefault("provider_followup_tasks", {})
        tasks[holder] = task

    async def _monitor_provider_turn(
        self,
        *,
        session: HarnessSession,
        context: dict[str, Any],
        event: asyncio.Event,
        watched_accounts: set[uuid.UUID],
    ) -> None:
        """Poll revocation and abort the root tree, then cancel its followers."""
        turn_id = context.get("provider_turn_id")
        try:
            while turn_id:
                state = await sync_to_async(self.ports.provider_turn_service.get)(
                    turn_id
                )
                if state is None or not state.active:
                    return
                if await sync_to_async(
                    self.ports.provider_turn_service.cancellation_reason
                )(turn_id):
                    message = (
                        "This response was stopped because its selected provider "
                        "account was removed or is no longer shared."
                    )
                    context["provider_revocation_message"] = message
                    await sync_to_async(
                        self.ports.provider_turn_service.set_monitor_required
                    )(turn_id)
                    for key in self.ports.descendant_keys(str(session.id)):
                        child = self.ports.run_registry.contexts.get(key)
                        if child is None or child.get("provider_turn_id") != turn_id:
                            continue
                        if child is not None:
                            child["provider_revocation_message"] = message
                        task = self.ports.owned_run_task(key)
                        if (
                            task is not None
                            and not task.done()
                            and not task.cancelling()
                        ):
                            task.cancel()
                    await self.ports.interactions.reject_sessions(
                        {
                            key
                            for key in self.ports.descendant_keys(str(session.id))
                            if self.ports.run_registry.contexts.get(key, {}).get(
                                "provider_turn_id"
                            )
                            == turn_id
                        }
                    )
                    root = self.ports.provider_turn_roots.get(str(turn_id), context)
                    root["provider_revocation_message"] = message
                    await sync_to_async(
                        self.ports.provider_turn_service.set_monitor_required
                    )(turn_id)
                    followers = tuple(root.get("provider_followup_tasks", {}).values())
                    for task in followers:
                        if not task.done() and not task.cancelling():
                            task.cancel()
                    for task in followers:
                        if task is asyncio.current_task() or task.done():
                            continue
                        try:
                            await asyncio.shield(task)
                        except (asyncio.CancelledError, Exception):
                            pass
                    while True:
                        state = await sync_to_async(
                            self.ports.provider_turn_service.get
                        )(turn_id)
                        if state is None or not state.active or not state.holds:
                            await sync_to_async(
                                self.ports.provider_turn_service.clear_monitor_required
                            )(turn_id)
                            return
                        await asyncio.sleep(0.1)
                event.clear()
                try:
                    await asyncio.wait_for(event.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("provider_turn_monitor_failed", session_id=str(session.id))
            for key, live in tuple(self.ports.run_registry.contexts.items()):
                if live.get("provider_turn_id") == turn_id:
                    self.ports.cancel_owned_task(key)
        finally:
            unregister_revocation_listener(watched_accounts, event=event)
            if turn_id:
                await self._maybe_finish_provider_monitor(context)
                state = await sync_to_async(self.ports.provider_turn_service.get)(
                    turn_id
                )
                if state is None or not state.active:
                    self.ports.provider_turn_roots.pop(str(turn_id), None)
