from django.apps import AppConfig


class ScheduledTasksConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.scheduled_tasks"
    verbose_name = "Scheduled tasks"

    def ready(self) -> None:
        """Subscribe task history to harness run events."""
        from . import signals  # noqa: F401
