from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class CodebaseConfig(AppConfig):
    name = "codebase"
    label = "codebase"
    verbose_name = _("Codebase")
