from django.apps import AppConfig
from django.utils.module_loading import autodiscover_modules
from django.utils.translation import gettext_lazy as _


class CodebaseConfig(AppConfig):
    name = "codebase"
    label = "codebase"
    verbose_name = _("Codebase")

    def ready(self):
        autodiscover_modules("clients.github.api.views")
        autodiscover_modules("clients.gitlab.api.views")
