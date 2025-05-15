from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AutomationConfig(AppConfig):
    name = "automation"
    label = "automation"
    verbose_name = _("Automation")
