from django.apps import AppConfig
from django.utils.module_loading import autodiscover_modules
from django.utils.translation import gettext_lazy as _


class QuickActionsConfig(AppConfig):
    name = "quick_actions"
    label = "quick_actions"
    verbose_name = _("Quick Actions")

    def ready(self):
        autodiscover_modules("actions")
