from __future__ import annotations

import uuid

from django.db import models
from django.utils.translation import gettext_lazy as _

from automation.agent.results import parse_agent_result


class ActivityStatus(models.TextChoices):
    READY = "READY", _("Pending")
    RUNNING = "RUNNING", _("Running")
    SUCCESSFUL = "SUCCESSFUL", _("Successful")
    FAILED = "FAILED", _("Failed")

    @classmethod
    def terminal(cls) -> frozenset[str]:
        return frozenset({cls.SUCCESSFUL, cls.FAILED})


class TriggerType(models.TextChoices):
    API_JOB = "api_job", _("API Job")
    MCP_JOB = "mcp_job", _("MCP Job")
    SCHEDULE = "schedule", _("Scheduled Job")
    ISSUE_WEBHOOK = "issue_webhook", _("Issue Webhook")
    MR_WEBHOOK = "mr_webhook", _("MR/PR Webhook")


class Activity(models.Model):
    """Unified record of every agent execution, regardless of trigger source.

    Denormalized fields (status, started_at, finished_at, result_summary,
    error_message, code_changes) ensure the record remains useful after
    the linked DBTaskResult row is pruned by the retention policy.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    trigger_type = models.CharField(_("trigger type"), max_length=20, choices=TriggerType.choices)
    task_result = models.OneToOneField(
        "django_tasks_database.DBTaskResult",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activity",
        verbose_name=_("task result"),
    )

    status = models.CharField(_("status"), max_length=10, choices=ActivityStatus.choices, default=ActivityStatus.READY)

    # Context fields
    repo_id = models.CharField(_("repository"), max_length=255)
    ref = models.CharField(_("branch / ref"), max_length=255, blank=True, default="")
    prompt = models.TextField(_("prompt"), blank=True, default="")

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

    # Denormalized timing
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    started_at = models.DateTimeField(_("started at"), null=True, blank=True)
    finished_at = models.DateTimeField(_("finished at"), null=True, blank=True)

    class Meta:
        verbose_name = _("Activity")
        verbose_name_plural = _("Activities")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["trigger_type", "-created_at"], name="activity_trigger_created_idx"),
            models.Index(fields=["repo_id", "-created_at"], name="activity_repo_created_idx"),
            models.Index(fields=["status", "-created_at"], name="activity_status_created_idx"),
            models.Index(fields=["scheduled_job", "-created_at"], name="activity_schedule_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.get_trigger_type_display()} on {self.repo_id} ({self.status})"

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

        if tr.status == ActivityStatus.FAILED and tr.exception_class_path and not self.error_message:
            self.error_message = tr.exception_class_path
            if tr.traceback:
                self.error_message += f"\n{tr.traceback}"
            changed.append("error_message")

        return changed
