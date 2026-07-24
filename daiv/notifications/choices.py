from django.db import models
from django.utils.translation import gettext_lazy as _


class NotifyOn(models.TextChoices):
    NEVER = "never", _("Never")
    ALWAYS = "always", _("Always")
    ON_SUCCESS = "on_success", _("On success only")
    ON_FAILURE = "on_failure", _("On failure only")


class ChannelType(models.TextChoices):
    EMAIL = "email", _("Email")
    ROCKETCHAT = "rocketchat", _("Rocket Chat")


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
    # Per-Run Feed row (Story 2.3): the Review Console's per-user "what happened" slice.
    # Written at Run granularity by ``emit_feed_on_run_finished`` and carved out of the bell
    # (unread count / dropdown / list / mark-all-read) so Feed and bell keep independent seen-state.
    RUN_FEED = "run.feed", _("Run feed")
