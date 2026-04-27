from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from notifications.choices import NotifyOn

from automation.agent.results import parse_agent_result

logger = logging.getLogger("daiv.activity")

if TYPE_CHECKING:
    from accounts.models import User


class ActivityStatus(models.TextChoices):
    READY = "READY", _("Pending")
    RUNNING = "RUNNING", _("Running")
    SUCCESSFUL = "SUCCESSFUL", _("Successful")
    FAILED = "FAILED", _("Failed")

    @classmethod
    def terminal(cls) -> frozenset[str]:
        return frozenset({cls.SUCCESSFUL, cls.FAILED})


class TriggerType(models.TextChoices):
    API_JOB = "api_job", _("API Run")
    MCP_JOB = "mcp_job", _("MCP Run")
    SCHEDULE = "schedule", _("Scheduled Run")
    UI_JOB = "ui_job", _("UI Run")
    ISSUE_WEBHOOK = "issue_webhook", _("Issue Webhook")
    MR_WEBHOOK = "mr_webhook", _("MR/PR Webhook")


class ActivityManager(models.Manager["Activity"]):
    def by_owner(self, user: User) -> models.QuerySet[Activity]:
        """Return activities visible to the given user.

        Admins see all. Regular users see activities where they are:
        - the owner (``user`` FK), or
        - matched by ``external_username``, or
        - a subscriber of the linked ``scheduled_job``.
        """
        if user.is_admin:
            return self.all()
        return self.filter(
            models.Q(user=user) | models.Q(external_username=user.username) | models.Q(scheduled_job__subscribers=user)
        ).distinct()

    def by_batch(self, batch_id) -> models.QuerySet[Activity]:
        """Return activities that share a ``batch_id`` (multi-repo submission group)."""
        return self.filter(batch_id=batch_id)


class Activity(models.Model):
    """Unified record of every agent execution, regardless of trigger source.

    Denormalized fields (status, started_at, finished_at, result_summary,
    error_message, code_changes) ensure the record remains useful after
    the linked DBTaskResult row is pruned by the retention policy.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    trigger_type = models.CharField(_("trigger type"), max_length=20, choices=TriggerType.choices)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activities",
        verbose_name=_("user"),
    )
    task_result = models.OneToOneField(
        "django_tasks_database.DBTaskResult",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activity",
        verbose_name=_("task result"),
    )

    status = models.CharField(_("status"), max_length=10, choices=ActivityStatus.choices, default=ActivityStatus.READY)

    batch_id = models.UUIDField(
        _("batch ID"),
        null=True,
        blank=True,
        db_index=True,
        help_text=_("Shared identifier for activities from the same submission."),
    )

    thread_id = models.CharField(
        _("thread ID"),
        max_length=64,
        null=True,
        blank=True,
        unique=True,
        db_index=True,
        help_text=_("LangGraph checkpoint key. Lets chat resume this run."),
    )

    external_username = models.CharField(
        _("external username"),
        max_length=255,
        blank=True,
        default="",
        help_text=_(
            "Git platform username from webhook payload."
            " Used for activity visibility matching and to backfill the user FK when they later join DAIV."
        ),
    )

    # Context fields
    repo_id = models.CharField(_("repository"), max_length=255)
    ref = models.CharField(_("branch / ref"), max_length=255, blank=True, default="")
    prompt = models.TextField(_("prompt"), blank=True, default="")
    use_max = models.BooleanField(_("use max model"), default=False)
    notify_on = models.CharField(  # noqa: DJ001 — null distinguishes "no override" from explicit "never".
        _("notify on"),
        max_length=16,
        choices=NotifyOn.choices,
        null=True,
        blank=True,
        help_text=_(
            "Per-run override. When null, the notifier falls back to ScheduledJob.notify_on"
            " (for schedule runs) or to the initiating user's notify_on_jobs preference."
        ),
    )

    # Issue / MR context
    issue_iid = models.PositiveIntegerField(_("issue IID"), null=True, blank=True)
    merge_request_iid = models.PositiveIntegerField(_("merge request IID"), null=True, blank=True)
    merge_request_web_url = models.URLField(_("merge request URL"), max_length=500, blank=True, default="")
    mention_comment_id = models.CharField(_("mention comment ID"), max_length=255, blank=True, default="")

    # Schedule linkage
    scheduled_job = models.ForeignKey(
        "schedules.ScheduledJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activities",
        verbose_name=_("scheduled job"),
    )

    # Denormalized result / error (survives DBTaskResult pruning)
    result_summary = models.TextField(_("result summary"), blank=True, default="")
    error_message = models.TextField(_("error message"), blank=True, default="")
    code_changes = models.BooleanField(_("code changes"), default=False)

    # Denormalized usage / cost (survives DBTaskResult pruning)
    input_tokens = models.PositiveIntegerField(_("input tokens"), null=True, blank=True)
    output_tokens = models.PositiveIntegerField(_("output tokens"), null=True, blank=True)
    total_tokens = models.PositiveIntegerField(_("total tokens"), null=True, blank=True)
    cost_usd = models.DecimalField(_("cost (USD)"), max_digits=10, decimal_places=6, null=True, blank=True)
    usage_by_model = models.JSONField(_("usage by model"), null=True, blank=True)

    # Denormalized timing
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    started_at = models.DateTimeField(_("started at"), null=True, blank=True)
    finished_at = models.DateTimeField(_("finished at"), null=True, blank=True)

    objects = ActivityManager()

    class Meta:
        verbose_name = _("Activity")
        verbose_name_plural = _("Activities")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["trigger_type", "-created_at"], name="activity_trigger_created_idx"),
            models.Index(fields=["repo_id", "-created_at"], name="activity_repo_created_idx"),
            models.Index(fields=["status", "-created_at"], name="activity_status_created_idx"),
            models.Index(fields=["scheduled_job", "-created_at"], name="activity_schedule_created_idx"),
            models.Index(fields=["user", "-created_at"], name="activity_user_created_idx"),
            models.Index(
                fields=["external_username", "-created_at"],
                name="activity_ext_user_created_idx",
                condition=models.Q(external_username__gt=""),
            ),
        ]
        constraints = [
            # ``thread_id`` is unique=True; "" would collide on the second insert
            # under Postgres (which treats NULL as not-equal but "" as a real
            # value). Forbid the empty-string sentinel so callers must use NULL.
            models.CheckConstraint(
                condition=models.Q(thread_id__isnull=True) | ~models.Q(thread_id=""), name="activity_thread_id_nonempty"
            )
        ]

    def __str__(self) -> str:
        return f"{self.get_trigger_type_display()} on {self.repo_id} ({self.status})"

    @property
    def effective_notify_on(self) -> NotifyOn:
        """Resolve the notification preference that applies to this run.

        Precedence: per-run override > schedule preference > user default > NEVER.
        """
        if self.notify_on:
            return NotifyOn(self.notify_on)
        if self.scheduled_job_id is not None and self.scheduled_job is not None:
            return NotifyOn(self.scheduled_job.notify_on)
        if self.user_id is not None and self.user is not None:
            return NotifyOn(self.user.notify_on_jobs)
        return NotifyOn.NEVER

    @property
    def is_retryable(self) -> bool:
        return self.status in ActivityStatus.terminal() and self.trigger_type not in {
            TriggerType.ISSUE_WEBHOOK,
            TriggerType.MR_WEBHOOK,
        }

    @property
    def duration(self) -> float | None:
        """Return the execution duration in seconds, or None if not finished."""
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None

    @property
    def response_text(self) -> str:
        """Return the response text from the task result, or the truncated denormalized summary if unavailable."""
        if self.task_result and self.task_result.return_value:
            parsed = parse_agent_result(self.task_result.return_value)
            if parsed["response"]:
                return parsed["response"]
        return self.result_summary

    def sync_and_save(self) -> bool:
        """Sync from the linked DBTaskResult and persist changed fields.

        Returns True if any field was updated (and a save was issued), else False.
        Emits ``activity_finished`` when the status transitions to a terminal state.

        Raises whatever ``sync_from_task_result`` or ``self.save`` raise — callers running
        in long-lived loops (signal handlers, management commands) must catch.
        """
        from activity.signals import emit_activity_finished_if_terminal

        previous_status = self.status
        changed = self.sync_from_task_result()
        if not changed:
            return False
        self.save(update_fields=changed)
        emit_activity_finished_if_terminal(self, previous_status=previous_status)
        return True

    def sync_from_task_result(self) -> list[str]:
        """Pull latest status/timing/result from the linked DBTaskResult.

        Returns:
            List of field names that were updated (empty if nothing changed).
        """
        if self.task_result is None:
            return []

        tr = self.task_result
        changed: list[str] = []

        for field, value in [("status", tr.status), ("started_at", tr.started_at), ("finished_at", tr.finished_at)]:
            if getattr(self, field) != value:
                setattr(self, field, value)
                changed.append(field)

        if tr.status == ActivityStatus.SUCCESSFUL and tr.return_value:
            parsed = parse_agent_result(tr.return_value)

            if parsed["response"] and not self.result_summary:
                self.result_summary = parsed["response"][:2000]
                changed.append("result_summary")
            if parsed["code_changes"] and not self.code_changes:
                self.code_changes = True
                changed.append("code_changes")
            if parsed["merge_request_id"] and not self.merge_request_iid:
                self.merge_request_iid = parsed["merge_request_id"]
                changed.append("merge_request_iid")
            if parsed["merge_request_web_url"] and not self.merge_request_web_url:
                self.merge_request_web_url = parsed["merge_request_web_url"]
                changed.append("merge_request_web_url")

            if (usage := parsed["usage"]) and self.input_tokens is None:
                if usage.get("input_tokens") is not None:
                    self.input_tokens = usage["input_tokens"]
                    changed.append("input_tokens")
                if usage.get("output_tokens") is not None:
                    self.output_tokens = usage["output_tokens"]
                    changed.append("output_tokens")
                if usage.get("total_tokens") is not None:
                    self.total_tokens = usage["total_tokens"]
                    changed.append("total_tokens")
                if usage.get("cost_usd") is not None:
                    try:
                        self.cost_usd = Decimal(usage["cost_usd"])
                    except Exception:
                        logger.warning("Invalid cost_usd value %r for activity %s", usage["cost_usd"], self.pk)
                    else:
                        changed.append("cost_usd")
                if usage.get("by_model") is not None:
                    self.usage_by_model = usage["by_model"]
                    changed.append("usage_by_model")

        if tr.status == ActivityStatus.FAILED and tr.exception_class_path and not self.error_message:
            self.error_message = tr.exception_class_path
            if tr.traceback:
                self.error_message += f"\n{tr.traceback}"
            changed.append("error_message")

        return changed
