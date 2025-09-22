from django.apps import AppConfig
from django.utils.module_loading import autodiscover_modules


class CoreConfig(AppConfig):
    name = "core"

    def ready(self):
        autodiscover_modules("checks")
        # Import sandbox module to register signal handlers
        from . import sandbox  # noqa: F401
