from django.db import models
from django.utils.translation import gettext_lazy as _


class NotifyOn(models.TextChoices):
    NEVER = "never", _("Never")
    ALWAYS = "always", _("Always")
    ON_SUCCESS = "on_success", _("On success only")
    ON_FAILURE = "on_failure", _("On failure only")


class DeliveryStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    SENT = "sent", _("Sent")
    FAILED = "failed", _("Failed")
    SKIPPED = "skipped", _("Skipped")
