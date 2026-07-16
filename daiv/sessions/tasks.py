import logging

from django.core.management import call_command
from django.db import IntegrityError
from django.utils.translation import gettext

from crontask import cron
from django_tasks import task

from core.utils import locked_task

logger = logging.getLogger("daiv.sessions")


@task(dedup=True)
async def classify_run_task(run_id: str) -> None:
    """Classify a finished scheduled run's prose report into its :class:`~sessions.models.RunEnvelope`.

    Enqueued by the ``classify_on_run_finished`` receiver (SCHEDULE-only, terminal-only). Runs
    out-of-band and never writes to or mutates ``Run``/``Run.task_result`` — it reads the run
    (including ``response_text``) as its input and the only row it creates is its own envelope.

    ``dedup=True`` (keyed on ``run_id``) plus the in-task ``aexists`` guard make classification
    idempotent: a duplicate ``run_finished`` delivery, retry, or manual re-enqueue writes exactly
    one envelope (the OneToOne would otherwise raise on a second insert).

    The load-bearing invariants are enforced here, in code, so no future method choice can break
    them: a FAILED run is a tooling problem (``failed``, no LLM call); a ``report``-intent run never
    yields a finding (``actionable == []``); a ``found-issues`` draft with no items is coerced to
    ``all-clear``; and — the reverse direction — only a ``found-issues`` envelope ever carries
    actionable items (any other status is emptied), so an off-contract draft can never persist an
    incoherent envelope. The classification *method*
    (:func:`sessions.classification.classify_response_text`) only proposes a draft.

    Precondition failures (missing run, no model configured) are log + return, leaving the run
    unclassified ("classifying…"). An unrecoverable method error propagates (task FAILED, no partial
    envelope written).
    """
    from core.site_settings import site_settings
    from schedules.models import Intent
    from sessions.classification import classify_response_text
    from sessions.envelopes import build_actionable_item, validate_actionable
    from sessions.models import EnvelopeStatus, Run, RunEnvelope, RunStatus

    run = (
        await Run.objects.select_related("task_result", "session", "session__scheduled_job").filter(pk=run_id).afirst()
    )
    if run is None:
        logger.warning("classify_run_task: run %s not found, skipping", run_id)
        return

    # Idempotency guard (AC6 / OneToOne safety): never double-write. Defense-in-depth alongside
    # ``dedup=True`` for a manual re-enqueue after the dedup row has aged out.
    if await RunEnvelope.objects.filter(run=run).aexists():
        return

    async def _persist(*, status: EnvelopeStatus, summary: str, actionable: list) -> None:
        # Write exactly one envelope. ``count`` is always ``len(actionable)`` (derived here so the two
        # can never disagree). The ``aexists`` guard above is check-then-act, so a concurrent task can
        # still race past it; the OneToOne then rejects the second insert with an ``IntegrityError``.
        try:
            await RunEnvelope.objects.acreate(
                run=run, status=status, count=len(actionable), summary=summary, actionable=actionable
            )
        except IntegrityError:
            # Only the documented race is a benign no-op: if an envelope now exists, a concurrent task
            # wrote it and we lost. Any *other* IntegrityError (a genuine constraint violation) must
            # surface as a FAILED task, not be silently disguised as a race — otherwise the run is left
            # unclassified with no signal.
            if await RunEnvelope.objects.filter(run=run).aexists():
                logger.debug("classify_run_task: envelope for run %s already exists (raced), skipping", run_id)
                return
            raise

    # Deterministic FAILED gating (AC5): a failed run is a tooling problem, decided before — and
    # without — any LLM call. Its prose report may be empty, so the summary comes from error_message.
    if run.status == RunStatus.FAILED:
        first_line = next((line.strip() for line in run.error_message.splitlines() if line.strip()), "")
        await _persist(status=EnvelopeStatus.FAILED, summary=first_line or gettext("Run failed."), actionable=[])
        return

    # Resolve intent defensively: a SCHEDULE-triggered run can still have ``scheduled_job is None``
    # if the schedule was deleted (``Session.scheduled_job`` is SET_NULL).
    schedule = run.session.scheduled_job if run.session_id else None
    intent = schedule.intent if schedule else Intent.WATCH_FIND

    model_names = tuple(
        model
        for model in (site_settings.run_classifier_model_name, site_settings.run_classifier_fallback_model_name)
        if model
    )
    if not model_names:
        # Both the model and its fallback resolved to empty (only via an explicit empty-string env
        # override). Documented precondition-failure skip rather than crashing on model_names[0].
        logger.error(
            "classify_run_task: no classifier model configured "
            "(check DAIV_RUN_CLASSIFIER_MODEL_NAME / _FALLBACK_MODEL_NAME), skipping run %s",
            run_id,
        )
        return

    # A SUCCESSFUL run can still have empty prose (e.g. a code-only run — ``response_text`` falls back
    # to an empty ``result_summary``). There is nothing to classify, and an empty prompt can make some
    # providers error, so write a calm ``all-clear`` directly instead of calling the method.
    text = run.response_text
    if not text.strip():
        await _persist(status=EnvelopeStatus.ALL_CLEAR, summary="", actionable=[])
        return

    draft = await classify_response_text(text, intent=intent, model_names=model_names)

    # Apply the load-bearing invariants in code (never delegated to the method), speaking the
    # ``EnvelopeStatus`` enum rather than raw strings so the vocabulary has a single source of truth:
    # a non-finding-bearing intent (report) never yields a *finding* (AC3) — a would-be ``found-issues``
    # coerces up to ``needs-attention``; a ``found-issues`` draft with no items coerces down to
    # ``all-clear`` (AC4). ``Intent.finding_bearing()`` is the enum's own source of truth for this.
    status = EnvelopeStatus(draft.status)
    if status == EnvelopeStatus.FOUND_ISSUES and intent not in Intent.finding_bearing():
        status = EnvelopeStatus.NEEDS_ATTENTION
    elif status == EnvelopeStatus.FOUND_ISSUES and not draft.actionable:
        status = EnvelopeStatus.ALL_CLEAR

    # Only ``found-issues`` carries items; every other status (incl. a coerced report, or an
    # off-contract draft that arrived with items) is emptied.
    drafted_items = draft.actionable if status == EnvelopeStatus.FOUND_ISSUES else []

    actionable = [
        build_actionable_item(id=str(index), kind=item.kind, label=item.label, ref=item.ref, fix_prompt=item.fix_prompt)
        for index, item in enumerate(drafted_items)
    ]
    # Pure validator (no DB I/O) — safe in async. NEVER call sync ``full_clean()`` here; the DB
    # ``run_envelope_status_valid`` CheckConstraint is the other persistence backstop.
    validate_actionable(actionable)

    await _persist(status=status, summary=draft.summary, actionable=actionable)


# Hardcoded like the other housekeeping crons (see core.tasks.prune_db_task_results_cron_task)
# rather than config-driven: this is a fixed-cadence crash-recovery backstop, and the sessions app
# has no conf.py to feed the import-time @cron schedule.
@cron("*/5 * * * *")
@task
@locked_task(key="sync-stuck-runs")
def sync_stuck_runs_cron_task():
    """Reconcile non-terminal Runs periodically (crash-recovery backstop).

    Re-syncs task-backed runs from their linked DBTaskResult and reaps inline chat runs a
    worker crash left stuck in RUNNING (once the session heartbeat goes stale). The normal
    path is the ``run_finished`` signal / streamer ``finally``; this is the safety net for
    missed transitions and hard crashes.

    ``locked_task`` (non-blocking) skips this tick if a prior run still holds the lock, so a
    pass that overruns the interval is never double-dispatched. The wrapped command raises
    ``CommandError`` on per-row failures, which fails this task's DBTaskResult so the error
    is visible to monitoring rather than silently swallowed.
    """
    call_command("sync_stuck_runs")
