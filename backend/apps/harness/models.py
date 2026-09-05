"""
Database models for the harness app.

Holds the org-wide LLM provider configuration. The API key is stored
Fernet-encrypted (same mechanism as the credentials app) and only
decrypted server-side when building a provider adapter.
"""

from __future__ import annotations

import uuid

from django.db import models


class ProviderConfig(models.Model):
    """
    Org-wide LLM provider configuration.

    Exactly one record per organization (OneToOne). Stores the
    encrypted provider API key plus endpoint and model defaults.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="harness_provider_config",
        help_text="Owning organization (exactly one config per org).",
    )
    api_key_encrypted = models.TextField(
        help_text="Fernet-encrypted provider API key.",
    )
    base_url = models.URLField(
        max_length=1024,
        default="https://openrouter.ai/api/v1",
        help_text="OpenAI-compatible API base URL.",
    )
    default_model = models.CharField(
        max_length=255,
        default="",
        blank=True,
        help_text="Default model for agentic runs.",
    )
    small_model = models.CharField(
        max_length=255,
        default="",
        blank=True,
        help_text="Cheaper model for title/compaction tasks.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "harness_provider_config"
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return f"ProviderConfig(org={self.organization_id})"
