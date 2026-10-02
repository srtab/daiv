import logging
from datetime import timedelta
from typing import Literal

from django.core.management import call_command
from django.db import IntegrityError
from django.utils import timezone
from django.utils.translation import gettext

from asgiref.sync import sync_to_async
from crontask import cron
from django_tasks import task

from core.constants import TASK_PRIORITY_TITLING, TASK_QUEUE_INTERACTIVE
from core.utils import locked_task

logger = logging.getLogger("daiv.sessions")

# Grace window so the reconciler never races the normal out-of-band classification of a just-finished
# run (the ``dedup=True`` + ``aexists`` guards already make a re-enqueue idempotent — this only trims churn).
RECLASSIFY_GRACE = timedelta(minutes=15)
# Bounded per tick so a backlog after a long provider/broker outage drains steadily rather than
# storming the queue in a single pass.
RECLASSIFY_BATCH_LIMIT = 200
# Recency floor so a first deploy of universal classification never sweeps every historical webhook/job
# run into paid classification; the notification relevance window reuses this same value.
RECLASSIFY_MAX_AGE = timedelta(hours=24)


@task(dedup=True, queue_name=TASK_QUEUE_INTERACTIVE)
async def classify_run_task(run_id: str) -> None:
    """Classify a finished non-chat run's prose report into its :class:`~sessions.models.RunEnvelope`.

    Enqueued by the ``classify_on_run_finished`` receiver (all non-chat origins, terminal-only). Runs
    out-of-band and never writes to or mutates ``Run``/``Run.task_result`` — it reads the run
    (including ``response_text``) as its input and the only row it creates is its own envelope.

    ``dedup=True`` (keyed on ``run_id``) plus the in-task ``aexists`` guard make classification
    idempotent: a duplicate ``run_finished`` delivery, retry, or manual re-enqueue writes exactly
    one envelope (the OneToOne would otherwise raise on a second insert).

    The load-bearing invariants are enforced here, in code, so no future method choice can break
    them: a FAILED run is a tooling problem (``failed``, no LLM call); a WAITING_INPUT run is
    ``needs-input`` with its question lines (no LLM call); a ``report``-intent run never
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
        await Run.objects
        # "user" is loaded because run_classified → resolve_recipients reads run.user for non-schedule runs.
        .select_related("task_result", "session", "session__scheduled_job", "session__scheduled_job__user", "user")
        .filter(pk=run_id)
        .afirst()
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
        from sessions.signals import run_classified  # local import: sessions.signals imports sessions.tasks

        try:
            envelope = await RunEnvelope.objects.acreate(
                run=run, status=status, count=len(actionable), summary=summary, actionable=actionable
            )
        except IntegrityError:
            # Only the documented race is a benign no-op: if an envelope now exists, a concurrent task
            # wrote it and we lost — the raced loser returns WITHOUT emitting (fire-once). Any other
            # IntegrityError must surface as a FAILED task, not be disguised as a race.
            if await RunEnvelope.objects.filter(run=run).aexists():
                logger.debug("classify_run_task: envelope for run %s already exists (raced), skipping", run_id)
                return
            raise
        # Emit on the success path only. Bridge to the sync executor: every receiver (notify() and its
        # recipient/subscriber queries) is synchronous ORM and would raise SynchronousOnlyOperation on
        # the event loop. thread_sensitive=True runs them in the shared sync executor.
        results = await sync_to_async(run_classified.send_robust, thread_sensitive=True)(
            sender=type(run), run=run, envelope=envelope
        )
        for recv, response in results:
            if isinstance(response, Exception):
                logger.error(
                    "run_classified receiver %s failed for run=%s",
                    getattr(recv, "__name__", recv),
                    run.pk,
                    exc_info=response,
                )

    # Deterministic FAILED gating (AC5): a failed run is a tooling problem, decided before — and
    # without — any LLM call. Its prose report may be empty, so the summary comes from error_message.
    if run.status == RunStatus.FAILED:
        first_line = next((line.strip() for line in run.error_message.splitlines() if line.strip()), "")
        await _persist(status=EnvelopeStatus.FAILED, summary=first_line or gettext("Run failed."), actionable=[])
        return

    if run.status == RunStatus.WAITING_INPUT:
        from automation.agent.questions import question_lines

        summary = question_lines(run.question)
        if not summary:
            logger.error("classify_run_task: run %s is waiting for input but has no question to show", run_id)
            summary = gettext("Waiting for your answer.")
        await _persist(status=EnvelopeStatus.NEEDS_INPUT, summary=summary, actionable=[])
        return

    # Defensive terminal-successful re-check: FAILED and WAITING_INPUT are handled above, so only a
    # SUCCESSFUL run may reach classification; anything else (a manual re-enqueue while RUNNING) skips.
    if run.status != RunStatus.SUCCESSFUL:
        logger.warning("classify_run_task: run %s is %s (not terminal-successful), skipping", run_id, run.status)
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


@cron("*/15 * * * *")
@task
@locked_task(key="reclassify-missing-envelopes")
def reclassify_missing_envelopes_cron_task():
    """Backstop the fire-once ``run_finished`` classification path (crash/failure recovery).

    ``run_finished`` emits exactly once per terminal transition, so a ``classify_run_task`` that
    failed unrecoverably (provider errors past all retries + fallback) or an ``.enqueue()`` that was
    dropped (a broker/DB blip, swallowed in ``classify_on_run_finished``) would otherwise strand the
    run at "classifying…" forever — nothing re-fires the signal. This periodic sweep re-targets
    terminal non-chat runs (all origins returned by ``get_classify_origins()``) that still have no
    ``RunEnvelope`` and re-enqueues classification, which is idempotent (``dedup=True`` + the in-task
    ``aexists`` guard) so re-enqueuing an in-flight or already-classified run is a safe no-op.
    A FAILED run with no envelope is likewise re-targeted and gets its ``failed`` envelope with no LLM
    call.

    Only ``classify_eligible`` runs are swept: pre-deploy rows are backfilled ineligible so a
    coverage-widening deploy never retro-classifies (and never retro-notifies) the backlog, while
    everything created afterward defaults eligible and stays a catch-all.

    Both the grace cutoff and the recency floor are keyed on ``finished_at``, not ``created_at``:
    batch siblings and orphan-queued recovery can make created_at→finished_at gaps exceed a day, so a
    ``created_at`` floor would permanently skip a run that finishes long after creation.

    ``locked_task`` (non-blocking) skips this tick if the prior one still holds the lock, so a pass
    that overruns the interval is never double-dispatched.
    """
    from sessions.models import Run, RunStatus
    from sessions.signals import get_classify_origins

    now = timezone.now()
    grace_cutoff = now - RECLASSIFY_GRACE
    age_floor = now - RECLASSIFY_MAX_AGE
    stranded_ids = list(
        Run.objects
        .filter(
            trigger_type__in=get_classify_origins(),
            status__in=RunStatus.terminal(),
            envelope__isnull=True,
            classify_eligible=True,
            # Keyed on finished_at (see docstring); terminal runs always have it set.
            finished_at__lt=grace_cutoff,
            finished_at__gte=age_floor,
        )
        .order_by("finished_at")
        .values_list("pk", flat=True)[:RECLASSIFY_BATCH_LIMIT]
    )
    for run_id in stranded_ids:
        classify_run_task.enqueue(str(run_id))
    if stranded_ids:
        logger.info("reclassify_missing_envelopes: re-enqueued %d stranded run(s)", len(stranded_ids))


@task(queue_name=TASK_QUEUE_INTERACTIVE)
async def evaluate_pipeline_watch_task(repo_id: str, ref: str, pipeline_id: int | None = None) -> None:
    """Judge CI for a watched branch and take the one action it implies.

    Short and user-visible, so it runs on the interactive queue rather than behind agent runs.
    """
    from sessions.pipeline_watch.service import PipelineWatch

    await PipelineWatch(repo_id).aevaluate(ref=ref, pipeline_id=pipeline_id)


@cron("*/10 * * * *")
@task
@locked_task(key="reconcile-pipeline-watches")
async def reconcile_pipeline_watches_cron_task():
    """Repair CI watches that webhook events did not resolve.

    Non-blocking lock, so a sweep that overruns the interval is never double-dispatched.
    """
    from sessions.pipeline_watch.reconciler import WatchReconciler

    touched = await WatchReconciler().areconcile()
    logger.info("reconcile_pipeline_watches: touched %d watches", touched)


@task(queue_name=TASK_QUEUE_INTERACTIVE, priority=TASK_PRIORITY_TITLING)
def generate_title_task(
    entity_type: Literal["session", "run"], pk: str, prompt: str, repo_id: str, ref: str = ""
) -> None:
    """Overwrite a Session/Run title with an LLM-generated one.

    Failures propagate to django-tasks (which logs + marks the task failed); the
    title set synchronously remains (heuristic for chat sessions, possibly empty
    for prompt-driven runs).
    """
    from automation.titling.llm import TitlerNotConfiguredError, generate_title
    from sessions.models import Run, Session

    model_cls = Session if entity_type == "session" else Run
    try:
        entity = model_cls.objects.get(pk=pk)
    except model_cls.DoesNotExist:
        logger.warning("generate_title_task: %s with pk=%s not found, skipping", entity_type, pk)
        return

    try:
        title = generate_title(
            prompt,
            repo_id=repo_id,
            ref=ref,
            run_metadata={"entity_type": entity_type, "entity_pk": pk, "repo_id": repo_id, "ref": ref},
        )
    except TitlerNotConfiguredError:
        logger.exception(
            "generate_title_task: model not configured for %s pk=%s — feature disabled until API key is set",
            entity_type,
            pk,
        )
        return

    entity.title = title
    entity.save(update_fields=["title"])


@task(queue_name=TASK_QUEUE_INTERACTIVE, priority=TASK_PRIORITY_TITLING)
def generate_batch_title_task(batch_id: str, prompt: str) -> None:
    """Generate a single LLM title for a Run batch and apply it to every untitled member.

    One LLM call per batch instead of one per run. Repo/ref context is omitted because batch
    members typically span repos. Only rows with an empty ``title`` are updated, so synchronous
    titles (e.g. scheduled-run templates) are preserved. The same title is stamped on each
    affected run's Session when the session title is still empty.
    """
    from automation.titling.llm import TitlerNotConfiguredError, generate_title
    from sessions.models import Run, Session

    try:
        title = generate_title(prompt, run_metadata={"entity_type": "run_batch", "batch_id": batch_id})
    except TitlerNotConfiguredError:
        logger.exception(
            "generate_batch_title_task: model not configured for batch_id=%s — feature disabled until API key is set",
            batch_id,
        )
        return

    updated = Run.objects.filter(batch_id=batch_id, title="").update(title=title)
    # Backfill the parent session title too — a fresh batch creates one session per run,
    # each with an empty title until this titler lands.
    Session.objects.filter(runs__batch_id=batch_id, title="").update(title=title)
    if updated == 0:
        logger.warning(
            "generate_batch_title_task: no rows updated for batch_id=%s (stale batch or all already titled)", batch_id
        )
    else:
        logger.info("generate_batch_title_task: updated %d runs for batch_id=%s", updated, batch_id)
