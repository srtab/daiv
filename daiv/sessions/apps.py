from django.apps import AppConfig


class SessionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "sessions"
    # "sessions" is taken by django.contrib.sessions; the label must differ.
    label = "agent_sessions"
    verbose_name = "Agent Sessions"

    def ready(self):
        import sessions.signals  # noqa: F401, PLC0415
