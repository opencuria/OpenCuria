"""Shared engine discovery/model business logic for REST, MCP and admission."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

from common.exceptions import NotFoundError

from ..provider_accounts import ProviderAccountService
from ..providers.base import ProviderError
from ..providers.models_catalog import (
    ProviderModel,
    list_merged_provider_models,
)
from .catalog import list_claude_models, list_engines
from .codex.catalog import (
    CODEX_EFFORTS,
    CODEX_PROVIDERS,
    chatgpt_models,
    normalize_codex_effort,
    normalize_codex_model,
)


class EngineCatalogService:
    """Expose only selected accounts and harness-compatible capabilities."""

    def list_engines(
        self, organization_id: uuid.UUID, user_id: int
    ) -> list[dict[str, Any]]:
        """Keep connection state scoped to the active organization and user."""
        selected = ProviderAccountService().selected_accounts(organization_id, user_id)
        connected = {p for p, account in selected.items() if account.credential_id}
        return list_engines(
            claude_connected="claude-agent" in connected,
            codex_connected=bool(connected.intersection(CODEX_PROVIDERS)),
        )

    def list_models(
        self,
        engine_id: str,
        organization_id: uuid.UUID,
        user_id: int,
        *,
        account_ids: dict[str, str] | None = None,
    ) -> list[ProviderModel]:
        """Use account/revision-scoped provider cache after current ACL checks."""
        if engine_id == "claude":
            return list_claude_models()
        if engine_id == "native":
            return list_merged_provider_models(
                organization_id=organization_id,
                user_id=user_id,
                account_ids=account_ids,
            )
        if engine_id != "codex":
            raise NotFoundError("HarnessEngine", engine_id)
        accounts = ProviderAccountService()
        if account_ids is None:
            selected = accounts.selected_accounts(organization_id, user_id)
            scoped_ids = {
                p: str(a.id) for p, a in selected.items() if p in CODEX_PROVIDERS
            }
        else:
            scoped_ids = {p: a for p, a in account_ids.items() if p in CODEX_PROVIDERS}
        # Authorize before cache access. Reuse normalized OpenRouter data, but
        # never the native static ChatGPT capabilities or unrelated providers.
        for provider, account_id in scoped_ids.items():
            account = accounts.get_account(organization_id, user_id, account_id)
            if account.provider != provider:
                raise ValueError("Provider account does not match Codex provider")
        rows = chatgpt_models() if "chatgpt" in scoped_ids else []
        if "openrouter" in scoped_ids:
            router = list_merged_provider_models(
                organization_id=organization_id,
                user_id=user_id,
                account_ids={"openrouter": scoped_ids["openrouter"]},
            )
            for model in router:
                if model.provider != "openrouter" or not model.supports_tools:
                    continue
                efforts = tuple(
                    e for e in model.reasoning_efforts if e in CODEX_EFFORTS
                )
                default = (
                    model.default_effort if model.default_effort in efforts else ""
                )
                rows.append(
                    replace(model, reasoning_efforts=efforts, default_effort=default)
                )
        return rows

    def validate_codex_config(
        self,
        organization_id: uuid.UUID,
        user_id: int,
        model: str | None,
        effort: str | None,
        *,
        provider: str | None = None,
        account_ids: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        """Reject unknown/non-tool models and provider switches before persistence."""
        normalized = normalize_codex_model(model or None, provider=provider)
        # Only consult this provider; a broken unrelated OpenRouter account must
        # not prevent a ChatGPT conversation from running (or vice versa).
        provider_id = normalized.split("/", 1)[0]
        if account_ids is None:
            account = (
                ProviderAccountService()
                .selected_accounts(organization_id, user_id)
                .get(provider_id)
            )
            scoped = {provider_id: str(account.id)} if account else {}
        else:
            scoped = (
                {provider_id: account_ids[provider_id]}
                if provider_id in account_ids
                else {}
            )
        from .codex.auth import resolve_codex_auth

        resolve_codex_auth(organization_id, user_id, scoped, normalized)
        try:
            rows = self.list_models(
                "codex", organization_id, user_id, account_ids=scoped
            )
        except ProviderError as exc:
            raise ValueError("Codex model catalog is temporarily unavailable") from exc
        spec = next((row for row in rows if row.id == normalized), None)
        if spec is None:
            raise ValueError(f"Unsupported Codex model {normalized!r}")
        return normalized, normalize_codex_effort(effort, spec)
