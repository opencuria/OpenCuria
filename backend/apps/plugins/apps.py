"""Django app configuration for the plugins app."""

from __future__ import annotations

from django.apps import AppConfig


class PluginsConfig(AppConfig):
    """Configuration for the plugins app."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.plugins"
    verbose_name = "Plugins"
