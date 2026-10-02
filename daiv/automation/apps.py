from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AutomationConfig(AppConfig):
    name = "automation"
    label = "automation"
    verbose_name = _("Automation")

    def ready(self) -> None:
        """Register the pydantic models the agent keeps in checkpointed state (``GitState.merge_request``)
        and connect the ``Provider`` receivers in ``automation.signals``."""
        from automation import signals  # noqa: F401
        from codebase.base import MergeRequest
        from core.checkpoint_types import register_checkpoint_type

        register_checkpoint_type(MergeRequest)
