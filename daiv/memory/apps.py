from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class MemoryConfig(AppConfig):
    name = "memory"
    verbose_name = _("Memory")

    def ready(self):
        import memory.signals  # noqa: F401
