from __future__ import annotations

from django.apps import AppConfig


class RunnersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.runners"
    verbose_name = "Runners"

    def ready(self) -> None:
        """Guard credentials that workspace plugins require against deletion."""
        from . import signals  # noqa: F401
