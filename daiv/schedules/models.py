from __future__ import annotations

from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from activity.services import validate_repo_list
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
    ONCE = "once", _("Once")


def _coerce_repos(repos, *, allow_empty: bool) -> list[dict]:
    """Validate a ``[{repo_id, ref}, ...]`` list and re-raise as a Django ``ValidationError``.

    With ``allow_empty=True`` an exactly-empty list is accepted; other malformed shapes
    (``None``, ``{}``, ``"x"``, ...) still flow through ``validate_repo_list`` so callers
    get a clear error instead of a silent reset to ``[]``.
    """
    if repos == [] and allow_empty:
        return []
    try:
        return validate_repo_list(repos)
    except ValueError as err:
        raise ValidationError({"repos": str(err)}) from err


def _validate_frequency_fields(
    *,
    frequency: Frequency,
    cron_expression: str,
    time: dt_time | None,
    run_at: datetime | None = None,
    require_run_at: bool = False,
) -> None:
    """Keep ``ScheduledJob`` and ``ScheduleTemplate`` clean-time rules in lockstep so values
    copied via ``ScheduleTemplate.to_schedule_kwargs()`` never produce a job the schedule rejects.

    ``require_run_at`` is set by ``ScheduledJob`` (which must store a date) and unset by
    ``ScheduleTemplate`` (which is a blueprint).
    """
    if frequency == Frequency.CUSTOM:
        if not cron_expression:
            raise ValidationError({"cron_expression": _("A cron expression is required for Custom frequency.")})
        if not croniter.is_valid(cron_expression):
            raise ValidationError({
                "cron_expression": _(
                    "Invalid cron expression. Use five space-separated fields: "
                    "minute hour day-of-month month day-of-week (e.g. '0 9 * * 1-5')."
                )
            })
    if frequency in (Frequency.DAILY, Frequency.WEEKDAYS, Frequency.WEEKLY) and not time:
        raise ValidationError({"time": _("Time is required for this frequency.")})
    if frequency == Frequency.ONCE:
        if require_run_at:
            if run_at is None:
                raise ValidationError({"run_at": _("Date and time is required for a one-off schedule.")})
            if run_at <= timezone.now() - timedelta(seconds=60):
                raise ValidationError({"run_at": _("The scheduled time must be in the future.")})
    elif run_at is not None:
        raise ValidationError({"run_at": _("Date and time only applies to one-off schedules.")})


class ScheduledJobManager(models.Manager["ScheduledJob"]):
    def by_owner(self, user: User) -> models.QuerySet[ScheduledJob]:
        """Return scheduled jobs visible to the given user.

        Admin users see all jobs; regular users see only their own.
        """
        if user.is_admin:
            return self.all()
        return self.filter(user=user)


class ScheduledJob(TimeStampedModel):
    """A user-defined schedule that runs the DAIV agent on 1-20 repositories.

    Target repositories are stored in ``repos`` as ``[{"repo_id": str, "ref": str}, ...]``.
    Each dispatch fans out into one agent run per entry, correlated by ``last_run_batch_id``.
    """

    objects = ScheduledJobManager()

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="scheduled_jobs", verbose_name=_("user")
    )
    name = models.CharField(_("name"), max_length=200)
    prompt = models.TextField(_("prompt"), help_text=_("What the agent should do."))
    repos = models.JSONField(
        _("repositories"),
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
    run_at = models.DateTimeField(
        _("run at"), null=True, blank=True, help_text=_("Specific date and time for one-off schedules.")
    )
    use_max = models.BooleanField(
        _("max mode"), default=False, help_text=_("Use the more capable model with thinking set to high.")
    )
    is_enabled = models.BooleanField(_("enabled"), default=True)
    next_run_at = models.DateTimeField(_("next run at"), null=True, blank=True, db_index=True)
    last_run_at = models.DateTimeField(_("last run at"), null=True, blank=True)
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
    source_template = models.ForeignKey(
        "schedules.ScheduleTemplate",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="schedules",
        verbose_name=_("source template"),
        help_text=_("Template this schedule was created from. Cleared if the template is later deleted."),
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

    DUPLICABLE_FIELDS = (
        "name",
        "prompt",
        "repos",
        "frequency",
        "cron_expression",
        "time",
        "run_at",
        "use_max",
        "notify_on",
    )

    def to_schedule_kwargs(self) -> dict:
        """Return the user-facing fields for the duplicate flow (owner/audit fields excluded)."""
        return {f: getattr(self, f) for f in self.DUPLICABLE_FIELDS}

    @property
    def is_fired_one_off(self) -> bool:
        """True once a ONCE schedule has fired — drives the read-only 'Fired' card state."""
        return self.frequency == Frequency.ONCE and self.run_count > 0

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        super().clean()
        self.repos = _coerce_repos(self.repos, allow_empty=False)
        _validate_frequency_fields(
            frequency=self.frequency,
            cron_expression=self.cron_expression,
            time=self.time,
            run_at=self.run_at,
            require_run_at=True,
        )

    def get_effective_cron(self) -> str:
        """Return the five-field cron expression for this schedule."""
        if self.frequency == Frequency.ONCE:
            raise ValueError("ONCE frequency has no cron expression")
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
        if self.frequency == Frequency.ONCE:
            if self.run_at is None:
                raise ValueError("ONCE frequency requires a run_at value")
            self.next_run_at = self.run_at
            return

        if after is None:
            after = timezone.now()

        local_now = timezone.localtime(after)
        cron_iter = croniter(self.get_effective_cron(), local_now)
        next_local = cron_iter.get_next(datetime)
        self.next_run_at = next_local.astimezone(UTC)


class ScheduleTemplate(TimeStampedModel):
    """An admin-curated blueprint users can start a scheduled job from.

    Templates are decoupled from schedules: values are copied at create time,
    so editing or deleting a template never affects existing schedules.
    """

    SCHEDULE_FIELDS = ("name", "prompt", "repos", "frequency", "cron_expression", "time", "use_max", "notify_on")
    # Coupled to ``to_picker_dict()``: every field read there must be in this
    # tuple or ``.only(*PICKER_FIELDS)`` queries will trigger a deferred-field
    # fetch per row. ``prompt`` is deliberately excluded.
    PICKER_FIELDS = (
        "id",
        "name",
        "description",
        "repos",
        "frequency",
        "cron_expression",
        "time",
        "use_max",
        "notify_on",
    )

    name = models.CharField(_("name"), max_length=200, unique=True)
    description = models.TextField(_("description"), blank=True, default="")
    prompt = models.TextField(_("prompt"), help_text=_("What the agent should do."))
    repos = models.JSONField(
        _("default repositories"),
        blank=True,
        default=list,
        help_text=_(
            "List of {repo_id, ref} entries that pre-fill the schedule's repo picker. Leave empty to let users choose."
        ),
    )
    frequency = models.CharField(_("frequency"), max_length=10, choices=Frequency.choices, default=Frequency.DAILY)
    cron_expression = models.CharField(_("cron expression"), max_length=100, blank=True, default="")
    time = models.TimeField(_("time"), null=True, blank=True)
    use_max = models.BooleanField(_("max mode"), default=False)
    notify_on = models.CharField(_("notify on"), max_length=16, choices=NotifyOn.choices, default=NotifyOn.NEVER)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_schedule_templates",
        verbose_name=_("created by"),
    )

    class Meta:
        verbose_name = _("Schedule Template")
        verbose_name_plural = _("Schedule Templates")
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(frequency=Frequency.CUSTOM) | ~models.Q(cron_expression=""),
                name="tpl_custom_requires_cron",
            )
        ]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        super().clean()
        self.repos = _coerce_repos(self.repos, allow_empty=True)
        _validate_frequency_fields(
            frequency=self.frequency, cron_expression=self.cron_expression, time=self.time, require_run_at=False
        )

    def to_schedule_kwargs(self) -> dict:
        return {f: getattr(self, f) for f in self.SCHEDULE_FIELDS}

    @property
    def frequency_summary(self) -> str:
        """Human-readable one-line cadence for the picker preview."""
        label = self.get_frequency_display()
        if self.frequency == Frequency.HOURLY:
            return str(_("Every hour"))
        if self.frequency == Frequency.CUSTOM:
            return str(_("Custom: %(cron)s") % {"cron": self.cron_expression})
        if self.frequency == Frequency.ONCE:
            return str(_("Once (pick a date)"))
        if self.time is not None:
            return str(_("%(label)s at %(time)s") % {"label": label, "time": self.time.strftime("%H:%M")})
        return str(label)

    @property
    def repos_summary(self) -> str:
        """One-line summary of default repos for the picker preview ("Any repo" when empty)."""
        if not self.repos:
            return str(_("Any repo"))
        first = self.repos[0]
        label = first["repo_id"]
        if first.get("ref"):
            label = f"{label} @ {first['ref']}"
        extra = len(self.repos) - 1
        if extra:
            return str(_("%(label)s +%(extra)d more") % {"label": label, "extra": extra})
        return label

    def to_picker_dict(self) -> dict:
        """Serialize into the JSON shape the gallery drawer consumes.

        Deliberately excludes ``prompt`` — the gallery shows the description only;
        the prompt flows through the server-side ``?template=<id>`` prefill path.
        """
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "repos": self.repos,
            "repos_summary": self.repos_summary,
            "frequency_display": self.get_frequency_display(),
            "frequency_summary": self.frequency_summary,
            "notify_on_display": self.get_notify_on_display(),
            "use_max": self.use_max,
        }
