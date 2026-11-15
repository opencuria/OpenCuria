from __future__ import annotations

from django.apps import AppConfig


class LicensingConfig(AppConfig):
    """Windows guest licensing: product key pool, user licenses, admission."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.licensing"
    verbose_name = "Licensing"
