"""Django app config for the harness app."""

from __future__ import annotations

from django.apps import AppConfig


class HarnessConfig(AppConfig):
    """App config for the agent harness."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.harness"
