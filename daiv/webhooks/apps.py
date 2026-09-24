from importlib import import_module

from django.apps import AppConfig


class WebhooksConfig(AppConfig):
    name = "webhooks"
    label = "webhooks"
    verbose_name = "Webhooks"

    def ready(self):
        """Register the platform callback views onto ``codebase.api.router``, which keeps the webhook URLs at
        ``/api/codebase/callbacks/{gitlab,github}``."""
        for platform in ("gitlab", "github"):
            import_module(f"webhooks.{platform}.views")
