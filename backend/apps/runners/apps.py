from __future__ import annotations

from django.apps import AppConfig


class RunnersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.runners"
    verbose_name = "Runners"
