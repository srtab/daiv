from django.apps import AppConfig
from django.utils.module_loading import autodiscover_modules
from django.utils.translation import gettext_lazy as _

from neomodel import config

from .conf import settings


class CodebaseConfig(AppConfig):
    name = "codebase"
    label = "codebase"
    verbose_name = _("Codebase")

    def ready(self):
        config.DATABASE_URL = settings.NEO4J_URL.get_secret_value()
        autodiscover_modules("graphs")
