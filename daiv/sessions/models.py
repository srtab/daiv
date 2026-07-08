from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from notifications.choices import NotifyOn

from automation.agent.results import parse_agent_result
from core.models import ThinkingLevelChoices
from sessions.managers import RunManager, SessionManager

logger = logging.getLogger("daiv.sessions")


class RunStatus(models.TextChoices):
    QUEUED = "QUEUED", _("Queued")
    READY = "READY", _("Pending")
    RUNNING = "RUNNING", _("Running")
    SUCCESSFUL = "SUCCESSFUL", _("Successful")
    FAILED = "FAILED", _("Failed")

    @classmethod
    def terminal(cls) -> frozenset[str]:
        return frozenset({cls.SUCCESSFUL, cls.FAILED})


class SessionOrigin(models.TextChoices):
    """How a session (or an individual run) was triggered.

    Shared by ``Session.origin`` (first trigger) and ``Run.trigger_type`` (per run):
    a webhook-origin session can later contain chat runs.
    Values for the non-chat members must stay identical to the old
    ``activity.TriggerType`` strings — dashboards deep-link ``?trigger=<value>``
    and the data migration copies them verbatim.
    """

    CHAT = "chat", _("Chat")
    API_JOB = "api_job", _("API Run")
    MCP_JOB = "mcp_job", _("MCP Run")
    SCHEDULE = "schedule", _("Scheduled Run")
    UI_JOB = "ui_job", _("UI Run")
    ISSUE_WEBHOOK = "issue_webhook", _("Issue Webhook")
    MR_WEBHOOK = "mr_webhook", _("MR/PR Webhook")


class Session(models.Model):
    """One agent thread. PK == LangGraph checkpoint key (``thread_id``)."""

    thread_id = models.CharField(max_length=64, primary_key=True)
    origin = models.CharField(_("origin"), max_length=20, choices=SessionOrigin.choices)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_sessions",
        verbose_name=_("user"),
    )
    external_username = models.CharField(_("external username"), max_length=255, blank=True, default="")
    repo_id = models.CharField(_("repository"), max_length=255)
    ref = models.CharField(_("branch / ref"), max_length=255, blank=True, default="")
    title = models.CharField(_("title"), max_length=120, blank=True, default="")
    agent_model = models.CharField(_("agent model"), max_length=255, blank=True, default="")
    agent_thinking_level = models.CharField(
        _("agent thinking level"), max_length=20, blank=True, default="", choices=ThinkingLevelChoices.choices
    )
    sandbox_environment = models.ForeignKey(
        "sandbox_envs.SandboxEnvironment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_sessions",
        verbose_name=_("sandbox environment"),
    )
    scheduled_job = models.ForeignKey(
        "schedules.ScheduledJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_sessions",
        verbose_name=_("scheduled job"),
    )
    issue_iid = models.PositiveIntegerField(_("issue IID"), null=True, blank=True)
    merge_request_iid = models.PositiveIntegerField(_("merge request IID"), null=True, blank=True)

    # Unified execution lock. NULL means "free slot"; any non-NULL value is the
    # holder id (AG-UI run_id for chat turns, str(Run.pk) for background runs).
    # Same semantics as the old ChatThread.active_run_id.
    active_run_id = models.CharField(max_length=64, null=True, blank=True, default=None)  # noqa: DJ001

    # default (not auto_now_add) so the data migration can backfill historical values.
    created_at = models.DateTimeField(_("created at"), default=timezone.now, editable=False)
    # Ordering + lock staleness. Bumped explicitly by the lock service and run creation
    # (not auto_now: queryset .aupdate() paths must control it, as ChatThread did).
    last_active_at = models.DateTimeField(_("last active at"), default=timezone.now)

    objects = SessionManager()

    class Meta:
        verbose_name = _("Session")
        verbose_name_plural = _("Sessions")
        ordering = ["-last_active_at"]
        indexes = [
            models.Index(fields=["user", "-last_active_at"], name="session_user_active_idx"),
            models.Index(fields=["origin", "-last_active_at"], name="session_origin_active_idx"),
            models.Index(fields=["repo_id", "-last_active_at"], name="session_repo_active_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(active_run_id__isnull=True) | ~models.Q(active_run_id=""),
                name="session_active_run_id_nonempty",
            ),
            # ``choices=`` is not enforced at the DB layer, and the lock writes via
            # ``.aupdate()`` (bypassing field validation), so pin the enum here too.
            models.CheckConstraint(condition=models.Q(origin__in=SessionOrigin.values), name="session_origin_valid"),
        ]

    def __str__(self) -> str:
        return str(self.title or self.thread_id)

    async def atouch(self) -> None:
        """Bump ``last_active_at`` (queryset update; safe from async contexts)."""
        await type(self).objects.filter(pk=self.pk).aupdate(last_active_at=timezone.now())


class Run(models.Model):
    """One agent execution within a session. Successor of ``activity.Activity``;
    UUIDs are preserved by the data migration so external job IDs keep resolving.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="runs", verbose_name=_("session"))
    trigger_type = models.CharField(_("trigger type"), max_length=20, choices=SessionOrigin.choices)
    status = models.CharField(_("status"), max_length=10, choices=RunStatus.choices, default=RunStatus.READY)
    task_result = models.OneToOneField(
        "django_tasks_database.DBTaskResult",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="run",
        verbose_name=_("task result"),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_runs",
        verbose_name=_("user"),
    )
    external_username = models.CharField(_("external username"), max_length=255, blank=True, default="")
    title = models.CharField(_("title"), max_length=120, blank=True, default="")
    batch_id = models.UUIDField(_("batch ID"), null=True, blank=True, db_index=True)
    repo_id = models.CharField(_("repository"), max_length=255)
    ref = models.CharField(_("branch / ref"), max_length=255, blank=True, default="")
    prompt = models.TextField(_("prompt"), blank=True, default="")
    agent_model = models.CharField(_("agent model"), max_length=255, blank=True, default="")
    agent_thinking_level = models.CharField(
        _("agent thinking level"), max_length=20, blank=True, default="", choices=ThinkingLevelChoices.choices
    )
    notify_on = models.CharField(  # noqa: DJ001 — null distinguishes "no override" from explicit "never".
        _("notify on"), max_length=16, choices=NotifyOn.choices, null=True, blank=True
    )
    mention_comment_id = models.CharField(_("mention comment ID"), max_length=255, blank=True, default="")
    merge_request_iid = models.PositiveIntegerField(_("merge request IID"), null=True, blank=True)
    merge_request_web_url = models.URLField(_("merge request URL"), max_length=500, blank=True, default="")
    sandbox_environment = models.ForeignKey(
        "sandbox_envs.SandboxEnvironment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_runs",
        verbose_name=_("sandbox environment"),
    )

    # Denormalized result / error / usage — copied verbatim from Activity.
    result_summary = models.TextField(_("result summary"), blank=True, default="")
    error_message = models.TextField(_("error message"), blank=True, default="")
    code_changes = models.BooleanField(_("code changes"), default=False)
    input_tokens = models.PositiveIntegerField(_("input tokens"), null=True, blank=True)
    output_tokens = models.PositiveIntegerField(_("output tokens"), null=True, blank=True)
    total_tokens = models.PositiveIntegerField(_("total tokens"), null=True, blank=True)
    cost_usd = models.DecimalField(_("cost (USD)"), max_digits=10, decimal_places=6, null=True, blank=True)
    usage_by_model = models.JSONField(_("usage by model"), null=True, blank=True)

    created_at = models.DateTimeField(_("created at"), default=timezone.now, editable=False)
    started_at = models.DateTimeField(_("started at"), null=True, blank=True)
    finished_at = models.DateTimeField(_("finished at"), null=True, blank=True)

    objects = RunManager()

    class Meta:
        verbose_name = _("Run")
        verbose_name_plural = _("Runs")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["session", "-created_at"], name="run_session_created_idx"),
            models.Index(fields=["trigger_type", "-created_at"], name="run_trigger_created_idx"),
            models.Index(fields=["status", "-created_at"], name="run_status_created_idx"),
            models.Index(fields=["user", "-created_at"], name="run_user_created_idx"),
        ]
        constraints = [
            # At most one active (READY or RUNNING) API/MCP run per session. QUEUED is
            # intentionally outside the constraint so FIFO siblings can stack; webhook
            # triggers share deterministic sessions and are intentionally excluded.
            # Exact port of activity_one_active_per_thread. The status/trigger_type
            # literals here must equal {RunStatus.READY, RunStatus.RUNNING} and
            # {SessionOrigin.API_JOB, SessionOrigin.MCP_JOB} — Django serializes
            # constraints with literals, so a drift is caught by
            # ``test_active_constraint_literals_match_enums``.
            models.UniqueConstraint(
                fields=["session"],
                condition=models.Q(status__in=["READY", "RUNNING"], trigger_type__in=["api_job", "mcp_job"]),
                name="run_one_active_per_session",
            ),
            # DB-level enum enforcement (``choices=`` alone is not enforced, and
            # ``.aupdate()``/raw writes bypass field validation).
            models.CheckConstraint(
                condition=models.Q(trigger_type__in=SessionOrigin.values), name="run_trigger_type_valid"
            ),
            models.CheckConstraint(condition=models.Q(status__in=RunStatus.values), name="run_status_valid"),
        ]

    def __str__(self) -> str:
        return f"{self.get_trigger_type_display()} on {self.repo_id} ({self.status})"

    @property
    def effective_notify_on(self) -> NotifyOn:
        if self.notify_on:
            return NotifyOn(self.notify_on)
        schedule = self.session.scheduled_job if self.session_id else None
        if schedule is not None:
            return NotifyOn(schedule.notify_on)
        if self.user_id is not None and self.user is not None:
            return NotifyOn(self.user.notify_on_jobs)
        return NotifyOn.NEVER

    @property
    def is_retryable(self) -> bool:
        return self.status in RunStatus.terminal() and self.trigger_type not in {
            SessionOrigin.ISSUE_WEBHOOK,
            SessionOrigin.MR_WEBHOOK,
            SessionOrigin.CHAT,
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
        Emits ``run_finished`` when the status transitions to a terminal state.

        Raises whatever ``sync_from_task_result`` or ``self.save`` raise — callers running
        in long-lived loops (signal handlers, management commands) must catch.
        """
        previous_status = self.status
        changed = self.sync_from_task_result()
        if not changed:
            return False
        self.save(update_fields=changed)
        from sessions.signals import emit_run_finished_if_terminal  # local import to avoid circular deps

        emit_run_finished_if_terminal(self, previous_status=previous_status)
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

        if tr.status == RunStatus.SUCCESSFUL and tr.return_value:
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
                        logger.warning("Invalid cost_usd value %r for run %s", usage["cost_usd"], self.pk)
                    else:
                        changed.append("cost_usd")
                if usage.get("by_model") is not None:
                    self.usage_by_model = usage["by_model"]
                    changed.append("usage_by_model")

        if tr.status == RunStatus.FAILED and tr.exception_class_path and not self.error_message:
            self.error_message = tr.exception_class_path
            if tr.traceback:
                self.error_message += f"\n{tr.traceback}"
            changed.append("error_message")

        return changed
