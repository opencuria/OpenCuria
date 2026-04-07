"""Django app configuration for the credentials app."""

from __future__ import annotations

from django.apps import AppConfig


class CredentialsConfig(AppConfig):
    """Configuration for the credentials app."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.credentials"
    verbose_name = "Credentials"
