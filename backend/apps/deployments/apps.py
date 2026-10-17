"""Django app configuration for deployment release records."""

from __future__ import annotations

from django.apps import AppConfig


class DeploymentsConfig(AppConfig):
    """Configuration for deployment compatibility state."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.deployments"
    verbose_name = "Deployment releases"
