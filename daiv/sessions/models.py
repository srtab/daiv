from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from notifications.choices import NotifyOn

from automation.agent.results import parse_agent_result
from core.models import ThinkingLevelChoices
from sessions.envelopes import validate_actionable
from sessions.managers import RunEnvelopeManager, RunManager, SessionManager

logger = logging.getLogger("daiv.sessions")


class RunStatus(models.TextChoices):
    QUEUED = "QUEUED", _("Queued")
    READY = "READY", _("Pending")
    RUNNING = "RUNNING", _("Running")
    SUCCESSFUL = "SUCCESSFUL", _("Done")
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

    @classmethod
    def webhooks(cls) -> frozenset[str]:
        """Trigger types originating from a git-platform webhook (issue / MR comment)."""
        return frozenset({cls.ISSUE_WEBHOOK, cls.MR_WEBHOOK})

    @classmethod
    def prompt_driven(cls) -> frozenset[str]:
        """Job triggers created from an explicit user prompt (API / MCP / UI batch submits)."""
        return frozenset({cls.API_JOB, cls.MCP_JOB, cls.UI_JOB})


class EnvelopeStatus(models.TextChoices):
    """The classification of a completed scheduled run (stored on ``RunEnvelope.status``).

    Values are hyphenated by deliberate convention — distinct from ``RunStatus``'s UPPER and
    ``SessionOrigin``'s snake_case, consistent with Story 1.1's ``intent`` (``watch-find``).
    Do not "normalize" them; the DB ``CheckConstraint`` pins these exact strings.
    """

    ALL_CLEAR = "all-clear", _("All clear")
    FOUND_ISSUES = "found-issues", _("Found issues")
    NEEDS_ATTENTION = "needs-attention", _("Needs attention")
    FAILED = "failed", _("Failed")


class OfferedAction(models.TextChoices):
    """The action the console offers for an envelope status (the FR-5 semantics).

    A stable action identifier the UI (Epic 5 ``button-fix``/``button-review``/``button-retry``)
    binds labels/colors to. It is **not** a stored field — it is derived per instance by
    :attr:`RunEnvelope.offered_action`.
    """

    NONE = "none", _("None")
    FIX = "fix", _("Fix it")
    REVIEW = "review", _("Review this")
    RETRY = "retry", _("Retry")


# The status -> offered-action mapping, defined exactly once (AC3/AC4); no call site
# recomputes it. ``FOUND_ISSUES`` is intentionally absent: its action depends on whether
# ``actionable`` is non-empty and is resolved in :attr:`RunEnvelope.offered_action`.
_STATUS_OFFERED_ACTION = {
    EnvelopeStatus.ALL_CLEAR: OfferedAction.NONE,
    EnvelopeStatus.NEEDS_ATTENTION: OfferedAction.REVIEW,
    EnvelopeStatus.FAILED: OfferedAction.RETRY,
}


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
            # Blank ("" = no override) or a valid ThinkingLevelChoices value.
            models.CheckConstraint(
                condition=models.Q(agent_thinking_level="")
                | models.Q(agent_thinking_level__in=ThinkingLevelChoices.values),
                name="session_agent_thinking_level_valid",
            ),
        ]

    def __str__(self) -> str:
        return str(self.title or self.thread_id)

    async def atouch(self) -> None:
        """Bump ``last_active_at`` (queryset update; safe from async contexts)."""
        await type(self).objects.filter(pk=self.pk).aupdate(last_active_at=timezone.now())


def usage_field_updates(usage: dict, *, run_ref: object) -> dict[str, Any]:
    """Map an agent usage summary to ``Run`` field updates (tokens, cost, per-model).

    Shared by :meth:`Run.sync_from_task_result` (task-backed runs) and the chat
    finalizer (``chat.api.streaming.finalize_chat_run``) so the two denormalization
    paths cannot drift. Only keys present (non-None) in ``usage`` produce updates;
    an unparseable ``cost_usd`` is logged against ``run_ref`` and skipped.
    """
    updates: dict[str, Any] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        if usage.get(key) is not None:
            updates[key] = usage[key]
    if usage.get("cost_usd") is not None:
        try:
            updates["cost_usd"] = Decimal(usage["cost_usd"])
        except Exception:
            logger.warning("Invalid cost_usd value %r for run %s", usage["cost_usd"], run_ref)
    if usage.get("by_model") is not None:
        updates["usage_by_model"] = usage["by_model"]
    return updates


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
    # AG-UI id of the human message that started this run; round-trips into the checkpoint
    # HumanMessage.id so sessions.transcript.annotate_transcript can join runs to turns.
    # Empty for background/legacy runs (they fall back to chronological ordinal matching).
    message_id = models.CharField(_("message ID"), max_length=255, blank=True, default="")
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
            # Blank ("" = no override) or a valid ThinkingLevelChoices value.
            models.CheckConstraint(
                condition=models.Q(agent_thinking_level="")
                | models.Q(agent_thinking_level__in=ThinkingLevelChoices.values),
                name="run_agent_thinking_level_valid",
            ),
            # NULL ("no override", distinct from explicit "never") or a valid NotifyOn value.
            models.CheckConstraint(
                condition=models.Q(notify_on__isnull=True) | models.Q(notify_on__in=NotifyOn.values),
                name="run_notify_on_valid",
            ),
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
        return self.status in RunStatus.terminal() and self.trigger_type not in (
            SessionOrigin.webhooks() | {SessionOrigin.CHAT}
        )

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

    def mark_failed(self, prefix: str, err: Exception) -> list[str]:
        """Set the terminal FAILED fields in-memory and return the ``update_fields`` list.

        Centralizes the exact field set + ``started_at`` backfill shared by the
        post-create failure paths (batch-submit enqueue/link failure in
        ``sessions.services`` and the dispatcher in ``sessions.signals``). The caller
        owns the save and the ``run_finished`` emit — this only mutates the instance.
        """
        now = timezone.now()
        self.status = RunStatus.FAILED
        self.error_message = f"{prefix}: {type(err).__name__}: {err}"
        self.finished_at = now
        if self.started_at is None:
            self.started_at = now
        return ["status", "error_message", "finished_at", "started_at"]

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
                for field, value in usage_field_updates(usage, run_ref=self.pk).items():
                    setattr(self, field, value)
                    changed.append(field)

        if tr.status == RunStatus.FAILED and tr.exception_class_path and not self.error_message:
            self.error_message = tr.exception_class_path
            if tr.traceback:
                self.error_message += f"\n{tr.traceback}"
            changed.append("error_message")

        return changed


class RunEnvelope(models.Model):
    """The structured classification of a completed scheduled ``Run``.

    The sole store of a run's classification (``status`` / ``count`` / ``summary`` /
    ``actionable[]``), OneToOne to its run. Read it via :meth:`RunEnvelopeManager.for_run`
    (None-safe: returns ``None`` for a still-classifying run) or the ``run.envelope`` reverse
    accessor (which raises ``DoesNotExist`` for a pending run). It is **not** a throughput/merge
    record — that is ``codebase.MergeMetric`` (AD-10), which this model never touches.
    """

    run = models.OneToOneField(Run, on_delete=models.CASCADE, related_name="envelope", verbose_name=_("run"))
    # No default: the classifier (Story 1.3) always sets a status; an unset value is invalid.
    status = models.CharField(_("status"), max_length=16, choices=EnvelopeStatus.choices, db_index=True)
    count = models.PositiveIntegerField(_("count"), default=0)
    # The classifier's one-line gloss — distinct from ``Run.result_summary`` (the full
    # agent-response fallback); do not conflate the two.
    summary = models.TextField(_("summary"), blank=True, default="")
    # The house list-JSON idiom (cf. ``codebase.topics``). The item contract lives in
    # ``sessions.envelopes`` (enforced by ``clean()``); never store filterable state inside.
    actionable = models.JSONField(_("actionable"), default=list, blank=True)
    # Mirrors ``Run.created_at``; supports Feed freshness/ordering and leaves room for a later
    # composite ``["status", "-created_at"]`` index without a rewrite.
    created_at = models.DateTimeField(_("created at"), default=timezone.now, editable=False)

    objects = RunEnvelopeManager()

    class Meta:
        verbose_name = _("Run Envelope")
        verbose_name_plural = _("Run Envelopes")
        # ``-id`` tiebreaker keeps Feed ordering/pagination deterministic when envelopes
        # share a ``created_at`` (cf. RunManager's ("-created_at", "-id") ordering).
        ordering = ["-created_at", "-id"]
        constraints = [
            # DB-level enum enforcement (``choices=`` alone is not enforced, and
            # ``.aupdate()``/raw writes bypass field validation). References
            # ``EnvelopeStatus.values`` directly so the constraint cannot drift.
            models.CheckConstraint(
                condition=models.Q(status__in=EnvelopeStatus.values), name="run_envelope_status_valid"
            )
        ]

    def __str__(self) -> str:
        return f"Envelope({self.status}) for run {self.run_id}"

    def save(self, *args, **kwargs):
        """Keep ``count`` a derived mirror of ``len(actionable)``.

        ``count`` is a queryable column (the Feed badge) but never an independently-authored value:
        deriving it here means no writer — an admin edit, a data fix, a future second producer — can
        persist a count that disagrees with the list. (The classifier task also sets it explicitly;
        this makes the coherence structural rather than convention.)
        """
        self.count = len(self.actionable)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields = set(update_fields)
            if "actionable" in update_fields:
                update_fields.add("count")
                kwargs["update_fields"] = update_fields
        super().save(*args, **kwargs)

    def clean(self) -> None:
        """Enforce the ``actionable[]`` contract and the FR-5 status<->actionable coherence invariant.

        Defense-in-depth at the model boundary: the Story 1.3 classifier already guarantees these,
        but ``full_clean()`` should independently reject an incoherent envelope so an admin edit, a
        raw ``RunEnvelope(...)`` build, or a future second writer cannot persist one. Both directions
        of the found-issues invariant are enforced here; ``count`` is kept coherent in ``save()``.
        """
        super().clean()
        validate_actionable(self.actionable)
        if self.status == EnvelopeStatus.FOUND_ISSUES and not self.actionable:
            raise ValidationError({"actionable": "A found-issues envelope must list at least one actionable item."})
        if self.status != EnvelopeStatus.FOUND_ISSUES and self.actionable:
            raise ValidationError({"actionable": "Only a found-issues envelope may carry actionable items."})

    @property
    def offered_action(self) -> OfferedAction:
        """The action the console offers for this envelope (the single mapping site, AC3/AC4)."""
        if self.status == EnvelopeStatus.FOUND_ISSUES:
            return OfferedAction.FIX if self.actionable else OfferedAction.NONE
        # ``.get`` (not ``[]``) so an unset/pending in-memory envelope resolves to NONE
        # rather than raising KeyError — persisted rows always have a constraint-valid status.
        return _STATUS_OFFERED_ACTION.get(self.status, OfferedAction.NONE)

    @property
    def is_actionable(self) -> bool:
        """Whether the console offers any action (Queue / Finding -> Fix gating)."""
        return self.offered_action != OfferedAction.NONE
