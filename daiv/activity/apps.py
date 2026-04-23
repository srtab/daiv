from django.apps import AppConfig


class ActivityConfig(AppConfig):
    name = "activity"

    def ready(self):
        import activity.signals  # noqa: F401
