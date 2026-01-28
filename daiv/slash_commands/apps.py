from django.apps import AppConfig
from django.utils.module_loading import autodiscover_modules
from django.utils.translation import gettext_lazy as _


class SlashCommandsConfig(AppConfig):
    name = "slash_commands"
    label = "slash_commands"
    verbose_name = _("Slash Commands")

    def ready(self):
        autodiscover_modules("actions")
