"""Small-model runner composition shared by metadata and commit suggestions."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from asgiref.sync import sync_to_async

from common.exceptions import NotFoundError

from .providers.base import ChatOptions, ProviderAdapter
from .providers.models_catalog import normalize_reasoning_effort
from .providers.resolver import StaticModelResolver
from .runner import HarnessRunner
from .services import ProviderConfigService
from .tools import default_tool_registry


async def build_small_model_runner(
    organization_id: uuid.UUID,
    *,
    user_id: int | None = None,
    account_ids: dict[str, str] | None = None,
    on_account_used: Callable[[str], None] | None = None,
    provider_factory: Callable[[uuid.UUID], ProviderAdapter] | None = None,
) -> tuple[HarnessRunner, str] | None:
    """Build a runner on the org's small model, or return None when unset.

    Resolve credentials off the event loop. Callers own account snapshots and
    may register provider use through ``on_account_used`` before each request.
    """
    config_service = ProviderConfigService()
    config = None
    try:
        config = await sync_to_async(config_service.get_config)(organization_id)
    except NotFoundError:
        config = None
    small_model = ((config.small_model if config else "") or "").strip()
    if not small_model:
        return None
    small_effort = normalize_reasoning_effort(
        (config.small_effort if config else "") or ""
    )
    if provider_factory is not None:
        model_resolver = StaticModelResolver(provider_factory(organization_id)).resolve
    else:
        resolver = await sync_to_async(config_service.build_resolver)(
            organization_id,
            user_id=user_id,
            account_ids=account_ids,
            on_account_used=on_account_used,
        )
        await sync_to_async(resolver.prepare_adapters)()
        model_resolver = resolver.resolve
    runner = HarnessRunner(
        model_resolver=model_resolver,
        tools=default_tool_registry(),
        chat_options=(
            ChatOptions(reasoning_effort=small_effort) if small_effort else None
        ),
    )
    return runner, small_model
