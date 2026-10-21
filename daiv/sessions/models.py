from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from automation.agent.references import ExternalRef, refs_from_stored  # noqa: TC001
from core.models import ThinkingLevelChoices
from sessions.artifacts import ArtifactKind, artifact_kind
from sessions.executor.result import parse_agent_result
from sessions.managers import RunArtifactManager, RunManager, SessionManager

if TYPE_CHECKING:
    from sessions.executor.result import AgentResult

logger = logging.getLogger("daiv.sessions")


class RunStatus(models.TextChoices):
    QUEUED = "QUEUED", _("Queued")
    READY = "READY", _("Pending")
    RUNNING = "RUNNING", _("Running")
    SUCCESSFUL = "SUCCESSFUL", _("Done")
    WAITING_INPUT = "WAITING_INPUT", _("Needs input")
    FAILED = "FAILED", _("Failed")

    @classmethod
    def terminal(cls) -> frozenset[str]:
        return frozenset({cls.SUCCESSFUL, cls.WAITING_INPUT, cls.FAILED})

    @classmethod
    def completed(cls) -> frozenset[str]:
        """Terminal statuses that close the work, unlike ``WAITING_INPUT``, which waits for the user's answer."""
        return frozenset({cls.SUCCESSFUL, cls.FAILED})


class SessionOrigin(models.TextChoices):
    """How a session (or an individual run) was triggered.

    Shared by ``Session.origin`` (first trigger) and ``Run.trigger_type`` (per run):
    a webhook-origin session can later contain chat runs.
    Values are stored on rows and deep-linked by dashboards (``?trigger=<value>``),
    so they must not change.
    """

    CHAT = "chat", _("Chat")
    API_JOB = "api_job", _("API Run")
    MCP_JOB = "mcp_job", _("MCP Run")
    SCHEDULE = "schedule", _("Scheduled Run")
    UI_JOB = "ui_job", _("UI Run")
    ISSUE_WEBHOOK = "issue_webhook", _("Issue Webhook")
    MR_WEBHOOK = "mr_webhook", _("MR/PR Webhook")
    PIPELINE_WEBHOOK = "pipeline_webhook", _("Pipeline Webhook")

    @classmethod
    def webhooks(cls) -> frozenset[str]:
        """Trigger types originating from a git-platform webhook (issue / MR comment)."""
        return frozenset({cls.ISSUE_WEBHOOK, cls.MR_WEBHOOK})

    @classmethod
    def prompt_driven(cls) -> frozenset[str]:
        """Job triggers created from an explicit user prompt (API / MCP / UI batch submits)."""
        return frozenset({cls.API_JOB, cls.MCP_JOB, cls.UI_JOB})

    @classmethod
    def classified(cls) -> frozenset[str]:
        """Non-chat origins that receive a RunEnvelope (SCHEDULE + webhooks + prompt-driven)."""
        return frozenset({cls.SCHEDULE}) | cls.webhooks() | cls.prompt_driven()

    @classmethod
    def unattended(cls) -> frozenset[str]:
        """Job triggers nobody is around to answer a question for."""
        return frozenset({cls.SCHEDULE, cls.PIPELINE_WEBHOOK})


class WatchState(models.TextChoices):
    """Lifecycle of a CI watch on a merge request DAIV published."""

    OFF = "off", _("Not watching")
    WATCHING = "watching", _("Waiting for CI")
    FIXING = "fixing", _("Fixing")
    GREEN = "green", _("Green")
    EXHAUSTED = "exhausted", _("Attempts exhausted")
    UNCLEAR = "unclear", _("CI unreadable")

    @classmethod
    def open(cls) -> frozenset[str]:
        """States the reconciler sweeps."""
        return frozenset({cls.WATCHING, cls.FIXING})


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
    mcp_overrides = models.JSONField(_("MCP overrides"), default=dict, blank=True)
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
    merge_request_iid = models.PositiveIntegerField(_("merge request IID"), null=True, blank=True)
    external_refs = models.JSONField(_("external references"), default=list, blank=True)
    watch_state = models.CharField(_("watch state"), max_length=10, choices=WatchState.choices, default=WatchState.OFF)
    watch_attempts = models.PositiveSmallIntegerField(_("watch attempts"), default=0)
    watch_pipeline_id = models.BigIntegerField(_("last watched pipeline"), null=True, blank=True)
    watch_armed_at = models.DateTimeField(_("watch armed at"), null=True, blank=True)

    # Unified execution lock. NULL means "free slot"; any non-NULL value is the
    # holder id (AG-UI run_id for chat turns, str(Run.pk) for background runs).
    active_run_id = models.CharField(max_length=64, null=True, blank=True, default=None)  # noqa: DJ001

    created_at = models.DateTimeField(_("created at"), default=timezone.now, editable=False)
    # Ordering + lock staleness. Bumped explicitly by the lock service and run creation
    # (not auto_now: queryset .aupdate() paths must control it).
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
            models.Index(fields=["repo_id", "ref", "watch_state"], name="session_watch_lookup_idx"),
            # The reconciler filters watch_armed_at with no repo_id, so the composite above
            # cannot serve it. Partial, because open watches are a rounding error on this table.
            models.Index(
                fields=["watch_armed_at"],
                name="session_watch_sweep_idx",
                condition=models.Q(watch_state__in=sorted(WatchState.open())),
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(active_run_id__isnull=True) | ~models.Q(active_run_id=""),
                name="session_active_run_id_nonempty",
            ),
            # ``choices=`` is not enforced at the DB layer, and the lock writes via
            # ``.aupdate()`` (bypassing field validation), so pin the enum here too.
            models.CheckConstraint(condition=models.Q(origin__in=SessionOrigin.values), name="session_origin_valid"),
            models.CheckConstraint(
                condition=models.Q(watch_state__in=WatchState.values), name="session_watch_state_valid"
            ),
            # Blank ("" = no override) or a valid ThinkingLevelChoices value.
            models.CheckConstraint(
                condition=models.Q(agent_thinking_level="")
                | models.Q(agent_thinking_level__in=ThinkingLevelChoices.values),
                name="session_agent_thinking_level_valid",
            ),
        ]

    def __str__(self) -> str:
        return str(self.title or self.thread_id)

    def merge_request_iids(self, *, limit: int = 2) -> set[int]:
        """Distinct merge requests this session reached, from its own column and its runs.

        The inverse of :meth:`SessionQuerySet.for_merge_request`; ``limit`` caps the run-side
        read for callers that only need to tell "exactly one" from "several".
        """
        run_iids = (
            self.runs
            .filter(merge_request_iid__isnull=False)
            .values_list("merge_request_iid", flat=True)
            .distinct()[:limit]
        )
        return {self.merge_request_iid, *run_iids} - {None}

    def touch(self) -> None:
        """Bump ``last_active_at`` (a queryset update, so no other column is written)."""
        type(self).objects.filter(pk=self.pk).update(last_active_at=timezone.now())

    def external_references(self) -> tuple[ExternalRef, ...]:
        """The stored refs as validated objects — the one route from this column into a run, so no
        reader reaches the raw JSON and skips the skip-the-malformed-entry parsing.
        """
        return refs_from_stored(self.external_refs)


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


def agent_result_field_updates(result: AgentResult) -> dict[str, Any]:
    """Map an agent result to the ``Run`` fields it fills, leaving out the ones it has no value for.

    Shared by the same two paths as :func:`usage_field_updates`, for the same reason.
    """
    updates = {
        "merge_request_iid": result["merge_request_id"],
        "merge_request_web_url": result["merge_request_web_url"],
    }
    return {field: value for field, value in updates.items() if value}


class Run(models.Model):
    """One agent execution within a session."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="runs", verbose_name=_("session"))
    trigger_type = models.CharField(_("trigger type"), max_length=20, choices=SessionOrigin.choices)
    status = models.CharField(_("status"), max_length=13, choices=RunStatus.choices, default=RunStatus.READY)
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
    muted = models.BooleanField(_("muted"), null=True, blank=True, default=None)  # null = inherit
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
    question = models.JSONField(_("question"), null=True, blank=True)
    input_tokens = models.PositiveIntegerField(_("input tokens"), null=True, blank=True)
    output_tokens = models.PositiveIntegerField(_("output tokens"), null=True, blank=True)
    total_tokens = models.PositiveIntegerField(_("total tokens"), null=True, blank=True)
    cost_usd = models.DecimalField(_("cost (USD)"), max_digits=10, decimal_places=6, null=True, blank=True)
    usage_by_model = models.JSONField(_("usage by model"), null=True, blank=True)
    clone_seconds = models.FloatField(_("clone seconds"), null=True, blank=True)
    sandbox_acquisition = models.CharField(_("sandbox acquisition"), max_length=16, blank=True, default="")

    created_at = models.DateTimeField(_("created at"), default=timezone.now, editable=False)
    started_at = models.DateTimeField(_("started at"), null=True, blank=True)
    finished_at = models.DateTimeField(_("finished at"), null=True, blank=True)
    # False only on pre-feature rows backfilled at deploy, so the classifier never retro-runs the
    # backlog; every new run is eligible. Chat is excluded by origin (SessionOrigin.classified), not this.
    classify_eligible = models.BooleanField(_("classify eligible"), default=True)

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
        ]

    def __str__(self) -> str:
        return f"{self.get_trigger_type_display()} on {self.repo_id} ({self.status})"

    @property
    def effective_muted(self) -> bool:
        if self.muted is not None:
            return self.muted
        schedule = self.session.scheduled_job if self.session_id else None
        if schedule is not None:
            return schedule.muted
        return False  # non-scheduled runs notify unless the run itself is muted

    @property
    def is_retryable(self) -> bool:
        return self.status in RunStatus.completed() and self.trigger_type not in (
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

    def sync_and_save(self) -> bool:
        """Sync from the linked DBTaskResult and persist changed fields.

        Returns True if any field was updated (and a write was issued and landed), else False.
        A transition to a terminal status is written by ``RunManager.finish``, which emits
        ``run_finished``. No write lands on a row that already finished.

        Raises whatever ``sync_from_task_result`` or the write raise — callers running
        in long-lived loops (signal handlers, management commands) must catch.
        """
        changed = self.sync_from_task_result()
        if not changed:
            return False
        fields = {name: getattr(self, name) for name in changed}
        if fields.get("status") in RunStatus.terminal():
            return type(self).objects.finish(self, fields.pop("status"), **fields)
        return type(self).objects.update_unfinished(self, **fields)

    def sync_from_task_result(self) -> list[str]:
        """Pull latest status/timing/result from the linked DBTaskResult.

        Returns:
            List of field names that were updated (empty if nothing changed).
        """
        if self.task_result is None:
            return []

        tr = self.task_result
        changed: list[str] = []
        parsed = parse_agent_result(tr.return_value) if tr.status == RunStatus.SUCCESSFUL and tr.return_value else None
        question = parsed["question"] if parsed is not None else None
        status = RunStatus.WAITING_INPUT if question else tr.status

        for field, value in [("status", status), ("started_at", tr.started_at), ("finished_at", tr.finished_at)]:
            if getattr(self, field) != value:
                setattr(self, field, value)
                changed.append(field)

        if parsed is not None:
            if parsed["response"] and not self.result_summary:
                self.result_summary = parsed["response"][:2000]
                changed.append("result_summary")
            if parsed["code_changes"] and not self.code_changes:
                self.code_changes = True
                changed.append("code_changes")
            for field, value in agent_result_field_updates(parsed).items():
                if not getattr(self, field):
                    setattr(self, field, value)
                    changed.append(field)
            if question and self.question is None:
                self.question = question
                changed.append("question")

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


def artifact_upload_to(instance: RunArtifact, filename: str, *, revision: str = "") -> str:
    """Storage name ``artifacts/<run_id>/<artifact_id>[-<revision>]<ext>``: unique per row and revision, so neither a
    re-published name nor a revision lands on a stored file, even on a storage that overwrites."""
    ext = PurePosixPath(filename).suffix.lower()[:16]
    suffix = f"-{revision}" if revision else ""
    return f"artifacts/{instance.run_id}/{instance.id}{suffix}{ext}"


class RunArtifact(models.Model):
    """A file the agent published out of a run's workspace: a report, a dataset, a rendered page.

    The bytes live in the default file storage (``MEDIA_ROOT``); the row carries what the viewer,
    the Jobs API and MCP expose. Deleting the run cascades to the row, and
    ``sessions.signals.delete_artifact_file`` removes the stored bytes. A later run of the session
    may revise the file in place (``updated_at``, ``revised_by``); ``run`` stays the run that first published it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="artifacts", verbose_name=_("run"))
    title = models.CharField(_("title"), max_length=200)
    filename = models.CharField(_("filename"), max_length=255)
    content_type = models.CharField(_("content type"), max_length=100)
    size = models.PositiveBigIntegerField(_("size"))
    file = models.FileField(_("file"), upload_to=artifact_upload_to, max_length=500)
    created_at = models.DateTimeField(_("created at"), default=timezone.now, editable=False)
    updated_at = models.DateTimeField(_("updated at"), null=True, blank=True, editable=False)
    revised_by = models.ManyToManyField(
        Run, blank=True, related_name="revised_artifacts", verbose_name=_("revised by"), editable=False
    )

    objects = RunArtifactManager()

    class Meta:
        verbose_name = _("Run Artifact")
        verbose_name_plural = _("Run Artifacts")
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"{self.filename} ({self.content_type}) for run {self.run_id}"

    def get_absolute_url(self) -> str:
        return reverse("session_artifact_detail", kwargs={"thread_id": self.run.session_id, "pk": self.pk})

    def get_raw_url(self) -> str:
        return reverse("session_artifact_raw", kwargs={"thread_id": self.run.session_id, "pk": self.pk})

    def get_download_url(self) -> str:
        return f"{self.get_raw_url()}?download=1"

    @property
    def kind(self) -> ArtifactKind:
        return artifact_kind(self.content_type)


class SessionArchive(models.Model):
    """One context of a session's agent state, as of the last run archived into it.

    Written by :func:`sessions.archive.aarchive_thread`, which also derives ``first_message_id`` and
    ``message_count`` from ``state``. A ``/clear`` sets ``closed_at``: the next state opens the next ``epoch``.
    """

    session = models.ForeignKey(
        Session, on_delete=models.CASCADE, related_name="archives", verbose_name=_("session"), db_index=False
    )
    epoch = models.PositiveIntegerField(_("epoch"), default=0)
    state = models.BinaryField(_("state"))
    state_type = models.CharField(_("state type"), max_length=16)
    first_message_id = models.CharField(_("first message ID"), max_length=255)
    message_count = models.PositiveIntegerField(_("message count"))
    closed_at = models.DateTimeField(_("closed at"), null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), default=timezone.now, editable=False)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Session Archive")
        verbose_name_plural = _("Session Archives")
        ordering = ["session_id", "epoch"]
        constraints = [
            models.UniqueConstraint(fields=["session", "epoch"], name="session_archive_epoch_unique"),
            models.CheckConstraint(condition=models.Q(message_count__gte=1), name="session_archive_has_messages"),
        ]

    def __str__(self) -> str:
        return f"Context {self.epoch} of session {self.session_id}"
