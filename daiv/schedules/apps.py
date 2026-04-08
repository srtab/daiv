from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class SchedulesConfig(AppConfig):
    name = "schedules"
    verbose_name = _("Schedules")
