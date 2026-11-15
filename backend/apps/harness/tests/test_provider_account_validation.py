"""Characterize OpenAI-compatible provider account validation policy."""

from __future__ import annotations

from typing import Any

import pytest

from apps.harness.models import ProviderConnection
from apps.harness.provider_accounts import ProviderAccountService


@pytest.mark.parametrize(
    ("models", "expected"),
    [([], []), ([" alpha ", "beta", "alpha", 42], ["alpha", "beta", "42"])],
)
def test_account_normalizes_manual_models(models: list, expected: list[str]) -> None:
    """Model IDs are trimmed, deduplicated in order, and stringified."""
    config = {"base_url": "https://provider.example/v1/", "models": models}
    ProviderAccountService.validate_credentials(
        "openai-compatible", {"models": models}, None, config, {}
    )
    assert config == {"base_url": "https://provider.example/v1", "models": expected}


@pytest.mark.parametrize(
    ("config", "error"),
    [
        ({}, "base_url is required"),
        ({"base_url": "ftp://provider.example/v1"}, "base_url"),
        ({"base_url": "https://"}, "base_url"),
        ({"models": "model"}, "models must be a list"),
        ({"models": [""]}, "models must not contain empty entries"),
        ({"models": [None]}, "models must not contain empty entries"),
        ({"models": [False]}, "models must not contain empty entries"),
    ],
)
def test_account_rejects_invalid_configuration(config: dict, error: str) -> None:
    """Invalid new account inputs keep their existing validation messages."""
    values = {"base_url": "https://provider.example/v1", **config}
    if not config:
        values = {}
    with pytest.raises(ValueError, match=error):
        ProviderAccountService.validate_credentials(
            "openai-compatible", config, None, values, {}
        )


def test_partial_account_update_tolerates_legacy_empty_models() -> None:
    """An unrelated edit can normalize old stored IDs without supplying new IDs."""
    existing = ProviderConnection(
        config={"base_url": "http://localhost:8080/v1/", "models": ["a", "", " a "]}
    )
    config: dict[str, Any] = {"base_url": "", "models": existing.config["models"]}
    ProviderAccountService.validate_credentials(
        "openai-compatible", {"label": "Renamed"}, existing, config, {}
    )
    assert config == {"base_url": "http://localhost:8080/v1", "models": ["a"]}

    with pytest.raises(ValueError, match="models must not contain empty entries"):
        ProviderAccountService.validate_credentials(
            "openai-compatible",
            {"models": [""]},
            existing,
            {"base_url": existing.config["base_url"], "models": [""]},
            {},
        )
