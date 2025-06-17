from django.apps import AppConfig
from django.utils.module_loading import autodiscover_modules
from django.utils.translation import gettext_lazy as _


class AutomationConfig(AppConfig):
    name = "automation"
    label = "automation"
    verbose_name = _("Automation")

    def ready(self):
        autodiscover_modules("tools.mcp.servers")
        # Import quick action implementations to ensure they are registered
        from automation.quick_actions.actions import hello_world  # noqa: F401
