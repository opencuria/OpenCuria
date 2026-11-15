"""
Enums for the harness app.
"""

from __future__ import annotations

from django.db import models


class ProviderType(models.TextChoices):
    """Supported LLM provider identifiers."""

    OPENROUTER = "openrouter", "OpenRouter"
    CHATGPT = "chatgpt", "ChatGPT"
    AMAZON_BEDROCK = "amazon-bedrock", "Amazon Bedrock"
    OPENAI_COMPATIBLE = "openai-compatible", "OpenAI Compatible"
