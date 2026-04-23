from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from croniter import croniter
from django_extensions.db.models import TimeStampedModel
from notifications.choices import NotifyOn

if TYPE_CHECKING:
    from accounts.models import User


class Frequency(models.TextChoices):
    HOURLY = "hourly", _("Hourly")
    DAILY = "daily", _("Daily")
    WEEKDAYS = "weekdays", _("Weekdays")
    WEEKLY = "weekly", _("Weekly")
    CUSTOM = "custom", _("Custom")


class ScheduledJobManager(models.Manager["ScheduledJob"]):
    def by_owner(self, user: User) -> models.QuerySet[ScheduledJob]:
        """Return scheduled jobs visible to the given user.

        Admin users see all jobs; regular users see only their own.
        """
        if user.is_admin:
            return self.all()
        return self.filter(user=user)


class ScheduledJob(TimeStampedModel):
    """A user-defined schedule that runs the DAIV agent on a repository."""

    objects = ScheduledJobManager()

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
    repos = models.JSONField(
        _("repositories"),
        default=list,
        blank=True,
        help_text=_("List of {repo_id, ref} entries. 1-20 entries. Empty ref means the default branch."),
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
    use_max = models.BooleanField(
        _("max mode"), default=False, help_text=_("Use the more capable model with thinking set to high.")
    )
    is_enabled = models.BooleanField(_("enabled"), default=True)
    next_run_at = models.DateTimeField(_("next run at"), null=True, blank=True, db_index=True)
    last_run_at = models.DateTimeField(_("last run at"), null=True, blank=True)
    last_run_task_id = models.UUIDField(_("last run task ID"), null=True, blank=True)
    last_run_batch_id = models.UUIDField(_("last run batch ID"), null=True, blank=True)
    run_count = models.PositiveIntegerField(_("run count"), default=0)
    notify_on = models.CharField(_("notify on"), max_length=16, choices=NotifyOn.choices, default=NotifyOn.NEVER)
    subscribers = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="subscribed_schedules",
        verbose_name=_("subscribers"),
        help_text=_("Other users CC'd on this schedule's finish notifications."),
    )

    class Meta:
        verbose_name = _("Scheduled Job")
        verbose_name_plural = _("Scheduled Jobs")
        ordering = ["-created"]
        indexes = [models.Index(fields=["is_enabled", "next_run_at"], name="sched_enabled_next_idx")]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(frequency=Frequency.CUSTOM) | ~models.Q(cron_expression=""),
                name="sched_custom_requires_cron",
            )
        ]

    MAX_REPOS = 20

    def __str__(self) -> str:
        return self.name

    def _validated_repos(self) -> list[dict]:
        repos = self.repos
        if not isinstance(repos, list) or not repos:
            raise ValidationError({"repos": _("At least one repository is required.")})
        if len(repos) > self.MAX_REPOS:
            raise ValidationError({
                "repos": _("A schedule can target at most %(n)d repositories.") % {"n": self.MAX_REPOS}
            })

        seen: set[tuple[str, str]] = set()
        normalized: list[dict] = []
        for entry in repos:
            if not isinstance(entry, dict) or set(entry.keys()) != {"repo_id", "ref"}:
                raise ValidationError({"repos": _("Each entry must be an object with keys 'repo_id' and 'ref'.")})
            repo_id = entry["repo_id"]
            ref = entry["ref"]
            if not isinstance(repo_id, str) or not repo_id.strip():
                raise ValidationError({"repos": _("repo_id must be a non-empty string.")})
            if not isinstance(ref, str):
                raise ValidationError({"repos": _("ref must be a string (empty for default branch).")})
            key = (repo_id, ref)
            if key in seen:
                raise ValidationError({"repos": _("Duplicate (repo_id, ref) entries are not allowed.")})
            seen.add(key)
            normalized.append({"repo_id": repo_id, "ref": ref})
        return normalized

    def clean(self) -> None:
        super().clean()
        self.repos = self._validated_repos()
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

        The next fire time is calculated in the project's local timezone so
        that DST transitions are handled correctly, then stored as UTC.

        Raises:
            ValueError: If the frequency/cron configuration is invalid.
        """
        if after is None:
            after = timezone.now()

        local_now = timezone.localtime(after)
        cron_iter = croniter(self.get_effective_cron(), local_now)
        next_local = cron_iter.get_next(datetime)
        self.next_run_at = next_local.astimezone(UTC)
