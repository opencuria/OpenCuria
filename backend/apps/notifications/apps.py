from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    """Connect mobile delivery to the harness's public lifecycle events."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.notifications"

    def ready(self) -> None:
        """Register consumers without coupling the harness to this app."""
        from . import signals  # noqa: F401
