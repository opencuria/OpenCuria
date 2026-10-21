"""Engine-specific dependencies for the common durable external run lifecycle."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

from asgiref.sync import sync_to_async


@dataclass
class ExternalRuntime:
    """Server-only dependencies; public state never includes engine options."""

    auth: Any = field(repr=False)
    session_store: Any
    default_model: str
    engine_options: dict[str, Any] = field(repr=False)
    _secrets: list[str] = field(default_factory=list, repr=False)

    def redact_error(self, error: object) -> str:
        """Bound and redact diagnostics before logging, never expose wire payloads."""
        from .shared.security import redact_text

        return redact_text(str(error), self._secrets)[:1000]

    @staticmethod
    def recovery_message(error: object) -> str:
        """Only explicitly public recovery errors may reach the chat client."""
        from .claude.errors import ClaudeTranscriptError
        from .codex.errors import CodexRecoveryError

        if isinstance(error, (ClaudeTranscriptError, CodexRecoveryError)):
            return str(error)
        return ""


async def prepare_external_runtime(
    *,
    service: Any,
    engine_id: str,
    session: Any,
    organization_id: uuid.UUID,
    context: dict[str, Any],
    ownership: Any,
    ensure_current: Callable[[], Awaitable[None]],
) -> ExternalRuntime:
    """Resolve one captured account and its engine's authoritative state adapter."""
    accounts = dict(context.get("account_ids") or {})
    user_id = context.get("initiated_by_id")
    turn_id = context.get("provider_turn_id")
    if engine_id == "claude":
        from .session_store import HarnessClaudeSessionStore

        account_id = accounts.get("claude-agent")
        await service._register_provider_use(turn_id, account_id)
        auth = await sync_to_async(service._resolve_engine_auth)(
            organization_id, user_id, account_id
        )
        return ExternalRuntime(
            auth=auth,
            session_store=HarnessClaudeSessionStore(
                session_id=session.id,
                external_session_id=session.external_session_id or "",
                project_key="",
            ),
            default_model="sonnet",
            engine_options={"auth_env": auth.secret_env},
            _secrets=[auth.token],
        )
    if engine_id != "codex":
        raise ValueError(f"Unsupported external harness {engine_id!r}")
    from ..providers.chatgpt_credentials import get_fresh_chatgpt_credentials
    from .codex.auth import resolve_codex_auth
    from .codex.catalog import DEFAULT_CODEX_MODEL, codex_provider
    from .codex.session_store import CodexSessionStore

    provider_id = codex_provider(session.model, session.engine_state)
    await service._register_provider_use(turn_id, accounts.get(provider_id))
    auth = await sync_to_async(resolve_codex_auth)(
        organization_id, user_id, accounts, session.model
    )
    if provider_id == "chatgpt":
        credentials = await get_fresh_chatgpt_credentials(
            organization_id, user_id, auth.connection_id
        )
        auth = replace(auth, credentials=credentials)
    await ensure_current()
    runtime = ExternalRuntime(
        auth=auth,
        session_store=CodexSessionStore(
            session_id=session.id,
            external_session_id=session.external_session_id or "",
            run_id=ownership.run_id,
            owner_token=ownership.owner_token,
        ),
        default_model=DEFAULT_CODEX_MODEL,
        engine_options={"auth": auth},
        _secrets=[str(value) for value in auth.credentials.values()],
    )

    async def refresh_auth() -> Any:
        """Refresh only this answer's captured account, never a new selection."""
        nonlocal auth
        await ensure_current()
        await service._register_provider_use(turn_id, str(auth.connection_id))
        credentials = await get_fresh_chatgpt_credentials(
            organization_id,
            user_id,
            auth.connection_id,
            rejected_access_token=str(auth.credentials.get("access") or ""),
        )
        await ensure_current()
        auth = replace(auth, credentials=credentials)
        runtime.auth = auth
        runtime._secrets.extend(str(value) for value in credentials.values())
        return auth

    runtime.engine_options["refresh_auth"] = refresh_auth
    return runtime
