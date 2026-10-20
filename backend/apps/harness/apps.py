"""Django app config for the harness app."""

from __future__ import annotations

from django.apps import AppConfig


class HarnessConfig(AppConfig):
    """App config for the agent harness."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.harness"

    def ready(self) -> None:
        """Register deletion signals and the runner-service gateway."""
        from apps.runners.services.infra.harness_gateway import (
            register_harness_gateway,
        )

        from . import signals  # noqa: F401
        from .runner_gateway import RunnerHarnessGateway

        register_harness_gateway(RunnerHarnessGateway())
