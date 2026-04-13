from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    name = "notifications"
    verbose_name = "Notifications"

    def ready(self):
        # Import signal receivers and channel registrations
        from notifications import signals  # noqa: F401
        from notifications.channels import email  # noqa: F401
