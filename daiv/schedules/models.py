from __future__ import annotations

import zoneinfo
from datetime import UTC, datetime

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from croniter import croniter
from django_extensions.db.models import TimeStampedModel


class Frequency(models.TextChoices):
    HOURLY = "hourly", _("Hourly")
    DAILY = "daily", _("Daily")
    WEEKDAYS = "weekdays", _("Weekdays")
    WEEKLY = "weekly", _("Weekly")
    CUSTOM = "custom", _("Custom")


class ScheduledJob(TimeStampedModel):
    """A user-defined schedule that runs the DAIV agent on a repository."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="scheduled_jobs", verbose_name=_("user")
    )
    name = models.CharField(_("name"), max_length=200)
    prompt = models.TextField(_("prompt"), help_text=_("What the agent should do."))
    repo_id = models.CharField(_("repository"), max_length=255, help_text=_("Repository identifier, e.g. owner/repo."))
    ref = models.CharField(
        _("branch / ref"),
        max_length=255,
        blank=True,
        default="",
        help_text=_("Git branch or ref. Leave blank for the default branch."),
    )
    frequency = models.CharField(_("frequency"), max_length=10, choices=Frequency.choices, default=Frequency.DAILY)
    cron_expression = models.CharField(
        _("cron expression"),
        max_length=100,
        blank=True,
        default="",
        help_text=_("Required when frequency is Custom. Five-field cron expression."),
    )
    time = models.TimeField(
        _("time"), null=True, blank=True, help_text=_("Time of day (used for Daily, Weekdays, and Weekly frequencies).")
    )
    timezone = models.CharField(
        _("timezone"), max_length=63, default="UTC", help_text=_("IANA timezone for the schedule.")
    )
    is_enabled = models.BooleanField(_("enabled"), default=True)
    next_run_at = models.DateTimeField(_("next run at"), null=True, blank=True, db_index=True)
    last_run_at = models.DateTimeField(_("last run at"), null=True, blank=True)
    last_run_task_id = models.UUIDField(_("last run task ID"), null=True, blank=True)
    run_count = models.PositiveIntegerField(_("run count"), default=0)

    class Meta:
        verbose_name = _("Scheduled Job")
        verbose_name_plural = _("Scheduled Jobs")
        ordering = ["-created"]
        indexes = [models.Index(fields=["is_enabled", "next_run_at"], name="sched_enabled_next_idx")]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(frequency="custom") | ~models.Q(cron_expression=""),
                name="sched_custom_requires_cron",
            )
        ]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        super().clean()
        if self.frequency == Frequency.CUSTOM:
            if not self.cron_expression:
                raise ValidationError({"cron_expression": _("A cron expression is required for Custom frequency.")})
            if not croniter.is_valid(self.cron_expression):
                raise ValidationError({
                    "cron_expression": _(
                        "Invalid cron expression. Use five space-separated fields: "
                        "minute hour day-of-month month day-of-week (e.g. '0 9 * * 1-5')."
                    )
                })
        if self.frequency not in (Frequency.HOURLY, Frequency.CUSTOM) and not self.time:
            raise ValidationError({"time": _("Time is required for this frequency.")})
        try:
            zoneinfo.ZoneInfo(self.timezone)
        except (KeyError, zoneinfo.ZoneInfoNotFoundError) as e:
            raise ValidationError({"timezone": _("Invalid timezone.")}) from e

    def get_effective_cron(self) -> str:
        """Return the five-field cron expression for this schedule."""
        if self.frequency == Frequency.CUSTOM:
            if not self.cron_expression:
                raise ValueError("Custom frequency requires a cron_expression")
            return self.cron_expression

        if self.frequency == Frequency.HOURLY:
            return "0 * * * *"

        if self.time is None:
            raise ValueError(f"Frequency '{self.frequency}' requires a time value")

        minute = self.time.minute
        hour = self.time.hour

        if self.frequency == Frequency.DAILY:
            return f"{minute} {hour} * * *"
        if self.frequency == Frequency.WEEKDAYS:
            return f"{minute} {hour} * * 1-5"
        if self.frequency == Frequency.WEEKLY:
            return f"{minute} {hour} * * 1"

        raise ValueError(f"Unknown frequency: {self.frequency}")

    def compute_next_run(self, after: datetime | None = None) -> None:
        """Compute and set ``next_run_at`` based on the cron expression and timezone.

        The next fire time is calculated in the schedule's local timezone so
        that DST transitions are handled correctly, then stored as UTC.

        Raises:
            zoneinfo.ZoneInfoNotFoundError: If the timezone is not valid.
            ValueError: If the frequency/cron configuration is invalid.
        """
        tz = zoneinfo.ZoneInfo(self.timezone)
        if after is None:
            after = datetime.now(tz=UTC)

        local_now = after.astimezone(tz)
        cron_iter = croniter(self.get_effective_cron(), local_now)
        next_local = cron_iter.get_next(datetime)
        self.next_run_at = next_local.astimezone(UTC)
