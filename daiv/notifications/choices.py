from django.db import models
from django.utils.translation import gettext_lazy as _


class ChannelType(models.TextChoices):
    EMAIL = "email", _("Email")
    ROCKETCHAT = "rocketchat", _("Rocket Chat")
    TELEGRAM = "telegram", _("Telegram")


class DeliveryStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    SENT = "sent", _("Sent")
    FAILED = "failed", _("Failed")
    SKIPPED = "skipped", _("Skipped")


class EventType(models.TextChoices):
    """Notification event identifiers in ``<snake_domain>.<past_participle>`` form."""

    JOB_FINISHED = "job.finished", _("Job finished")
    SCHEDULE_FINISHED = "schedule.finished", _("Schedule finished")
    JOB_BATCH_FINISHED = "job_batch.finished", _("Job batch finished")
    PIPELINE_WATCH_EXHAUSTED = "pipeline_watch.exhausted", _("Pipeline watch gave up")
