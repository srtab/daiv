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
    SCHEDULE_DISPATCH_FAILED = "schedule_dispatch.failed", _("Schedule can't run")
    INBOX_PAUSED = "inbox.paused", _("Session paused after runs in a row")


class EnvelopeStatus(models.TextChoices):
    """The classification of a terminal run (stored on ``RunEnvelope.status``).

    Values are hyphenated by deliberate convention — distinct from ``RunStatus``'s UPPER and
    ``SessionOrigin``'s snake_case, consistent with Story 1.1's ``intent`` (``watch-find``).
    Do not "normalize" them; the DB ``CheckConstraint`` pins these exact strings.
    """

    ALL_CLEAR = "all-clear", _("All clear")
    FOUND_ISSUES = "found-issues", _("Found issues")
    NEEDS_ATTENTION = "needs-attention", _("Needs attention")
    NEEDS_INPUT = "needs-input", _("Needs input")
    FAILED = "failed", _("Failed")

    @classmethod
    def worst_first(cls) -> tuple[EnvelopeStatus, ...]:
        """Severity ranking, worst first. Load-bearing wherever a list of runs is capped: unordered
        rows let a rollup show three needs-attention repos and hide the failure."""
        return (cls.FAILED, cls.NEEDS_INPUT, cls.FOUND_ISSUES, cls.NEEDS_ATTENTION, cls.ALL_CLEAR)
