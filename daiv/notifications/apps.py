from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class NotificationsConfig(AppConfig):
    name = "notifications"
    verbose_name = _("Notifications")

    def ready(self):
        # Import signal receivers; channel modules self-register on import of the channels package.
        from notifications import channels, signals  # noqa: F401
