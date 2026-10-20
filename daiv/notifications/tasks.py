from __future__ import annotations

import logging
from datetime import timedelta
from uuid import UUID

from django.db import IntegrityError
from django.db.models import Count, Q
from django.urls import NoReverseMatch
from django.utils import timezone
from django.utils.translation import gettext

from asgiref.sync import sync_to_async
from crontask import cron
from django_tasks import task
from sessions.models import Run, RunStatus, SessionOrigin

from core.constants import TASK_PRIORITY_NOTIFICATION, TASK_QUEUE_INTERACTIVE
from core.site_settings import site_settings
from core.utils import locked_task
from notifications.channels.registry import get_channel
from notifications.choices import DeliveryStatus, EnvelopeStatus
from notifications.classification.envelopes import build_actionable_item, validate_actionable
from notifications.exceptions import UnknownChannelError, UnrecoverableDeliveryError
from notifications.models import Notification, NotificationDelivery, RunEnvelope
from notifications.policy import (
    NOTIFY_WORTHY_STATUSES,
    RECLASSIFY_MAX_AGE,
    notification_source_for_run,
    notifies,
    within_relevance_window,
)
from notifications.run_notifiers import emit_run_notification, resolve_recipients
from schedules.models import Intent

logger = logging.getLogger("daiv.notifications")

MAX_DELIVERY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = [60, 300]  # wait before attempt 2, before attempt 3
MAX_RETRY_AFTER_SECONDS = 3600


def _retry_delay(exc: Exception, attempts: int) -> int:
    """Seconds to wait before the next attempt.

    The ladder is the floor; a channel whose provider named its own wait (Telegram's flood
    control) pushes the retry out to it, capped so one bad value cannot park a delivery for days.
    """
    backoff = RETRY_BACKOFF_SECONDS[min(attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
    retry_after = getattr(exc, "retry_after", None)
    # bool is an int subclass, and the value reaches us from a remote response body.
    if isinstance(retry_after, bool) or not isinstance(retry_after, int | float) or retry_after <= 0:
        return backoff
    return max(backoff, min(int(retry_after), MAX_RETRY_AFTER_SECONDS))


def _deliver_notification(delivery_id: UUID) -> None:
    """Execute a single delivery attempt. Called by the django-tasks worker and by tests."""
    try:
        delivery = NotificationDelivery.objects.select_related("notification__recipient").get(id=delivery_id)
    except NotificationDelivery.DoesNotExist:
        logger.warning("Delivery %s no longer exists, skipping", delivery_id)
        return

    if delivery.status != DeliveryStatus.PENDING:
        logger.info("Delivery %s has status=%s, skipping", delivery_id, delivery.status)
        return

    delivery.attempts += 1
    delivery.last_attempted_at = timezone.now()
    delivery.save(update_fields=["attempts", "last_attempted_at", "modified"])

    try:
        channel = get_channel(delivery.channel_type)
    except UnknownChannelError as exc:
        delivery.mark_skipped(str(exc))
        return

    try:
        channel.send(delivery.notification, delivery)
    except UnrecoverableDeliveryError as exc:
        logger.warning("Unrecoverable failure delivering %s: %s", delivery_id, exc)
        delivery.mark_failed(str(exc))
        return
    except Exception as exc:
        logger.exception(
            "Transient failure delivering %s via %s (attempt %d/%d)",
            delivery_id,
            delivery.channel_type,
            delivery.attempts,
            MAX_DELIVERY_ATTEMPTS,
        )
        if delivery.attempts >= MAX_DELIVERY_ATTEMPTS:
            delivery.mark_failed(str(exc))
            return
        # Stay PENDING; re-enqueue with backoff
        delivery.error_message = str(exc)
        delivery.save(update_fields=["error_message", "modified"])
        run_after = timezone.now() + timedelta(seconds=_retry_delay(exc, delivery.attempts))
        try:
            deliver_notification_task.using(run_after=run_after).enqueue(str(delivery.id))
        except Exception:
            logger.exception("Failed to re-enqueue delivery %s for retry", delivery_id)
            delivery.mark_failed(f"Re-enqueue failed after attempt {delivery.attempts}")
        return

    delivery.mark_sent()


@task(queue_name=TASK_QUEUE_INTERACTIVE, priority=TASK_PRIORITY_NOTIFICATION)
def deliver_notification_task(delivery_id: str) -> None:
    """Public task entry point -- thin wrapper so tests can call ``_deliver_notification`` directly."""
    _deliver_notification(UUID(delivery_id))


# Grace window so the reconciler never races the normal out-of-band classification of a just-finished
# run (the ``dedup=True`` + ``aexists`` guards already make a re-enqueue idempotent — this only trims churn).
RECLASSIFY_GRACE = timedelta(minutes=15)
# Bounded per tick so a backlog after a long provider/broker outage drains steadily rather than
# storming the queue in a single pass.
RECLASSIFY_BATCH_LIMIT = 200


def _notify_classified(run, envelope) -> None:
    """Deliver a classified run's notifications; a delivery failure never fails the caller."""
    try:
        emit_run_notification(run, envelope)
    except Exception:
        logger.exception("Notifying for classified run=%s failed", run.pk)


@task(dedup=True, queue_name=TASK_QUEUE_INTERACTIVE)
async def classify_run_task(run_id: str) -> None:
    """Classify a finished non-chat run's prose report into its :class:`~notifications.models.RunEnvelope`.

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
    (:func:`notifications.classification.llm.classify_response_text`) only proposes a draft.

    Precondition failures (missing run, no model configured) are log + return, leaving the run
    unclassified ("classifying…"). An unrecoverable method error propagates (task FAILED, no partial
    envelope written).
    """
    from notifications.classification.llm import classify_response_text

    run = (
        await Run.objects
        # "user" is loaded because the notification's resolve_recipients reads run.user for non-schedule runs.
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
        # The ``aexists`` guard above is check-then-act, so a concurrent task can still race past it;
        # the OneToOne then rejects the second insert with an ``IntegrityError``.
        try:
            envelope = await RunEnvelope.objects.acreate(run=run, status=status, summary=summary, actionable=actionable)
        except IntegrityError:
            # Only the lost race is benign, and its loser must not notify (fire-once); any other
            # IntegrityError fails the task.
            if await RunEnvelope.objects.filter(run=run).aexists():
                logger.debug("classify_run_task: envelope for run %s already exists (raced), skipping", run_id)
                return
            raise
        # Notify on the success path only. The notifier and its recipient/subscriber queries are
        # synchronous ORM, so they run in the shared sync executor rather than on the event loop.
        await sync_to_async(_notify_classified, thread_sensitive=True)(run, envelope)

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
        build_actionable_item(id=str(index), kind=item.kind, label=item.label, ref=item.ref)
        for index, item in enumerate(drafted_items)
    ]
    # Pure validator (no DB I/O) — safe in async. NEVER call sync ``full_clean()`` here; the DB
    # ``run_envelope_status_valid`` CheckConstraint is the other persistence backstop.
    validate_actionable(actionable)

    await _persist(status=status, summary=draft.summary, actionable=actionable)


@cron("*/15 * * * *")
@task
@locked_task(key="reclassify-missing-envelopes")
def reclassify_missing_envelopes_cron_task():
    """Backstop the fire-once ``run_finished`` classification path (crash/failure recovery).

    ``run_finished`` emits exactly once per terminal transition, so a ``classify_run_task`` that
    failed unrecoverably (provider errors past all retries + fallback) or an ``.enqueue()`` that was
    dropped (a broker/DB blip, swallowed in ``classify_on_run_finished``) would otherwise strand the
    run at "classifying…" forever — nothing re-fires the signal. This periodic sweep re-targets
    terminal non-chat runs (all origins returned by ``SessionOrigin.classified()``) that still have no
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
    now = timezone.now()
    grace_cutoff = now - RECLASSIFY_GRACE
    age_floor = now - RECLASSIFY_MAX_AGE
    stranded_ids = list(
        Run.objects
        .filter(
            trigger_type__in=SessionOrigin.classified(),
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


REDRIVE_BATCH_LIMIT = 200


@cron("*/15 * * * *")
@task
@locked_task(key="redrive-missing-notifications")
def redrive_missing_notifications_cron_task():
    """Close the crash window between envelope-persist and notification delivery.

    If a worker dies after the envelope is written but before its notification is delivered, the run has
    an envelope (so the reclassify backstop skips it) and the notification would be lost forever. This
    sweep re-emits the notification for classified, notify-worthy, in-window, un-muted runs that are still
    missing at least one recipient's Notification row; the per-run/batch unique constraints make the
    re-emit idempotent, so recipients that already have a row are left untouched.

    ``locked_task`` (non-blocking) skips this tick if the prior one still holds the lock.
    """

    def _delivered(source_type: str, source_id: str, event_type: str) -> set:
        return set(
            Notification.objects.filter(
                source_type=source_type, source_id=source_id, event_type=event_type
            ).values_list("recipient_id", flat=True)
        )

    now = timezone.now()
    candidates = (
        Run.objects
        .filter(
            trigger_type__in=SessionOrigin.classified(),
            status__in=RunStatus.terminal(),
            classify_eligible=True,
            envelope__isnull=False,
            envelope__status__in=NOTIFY_WORTHY_STATUSES,
            finished_at__isnull=False,
            finished_at__gte=now - RECLASSIFY_MAX_AGE,
        )
        .exclude(envelope__status=EnvelopeStatus.NEEDS_INPUT, trigger_type__in=SessionOrigin.webhooks())
        .select_related("envelope", "session", "session__scheduled_job", "session__scheduled_job__user", "user")
        .prefetch_related("session__scheduled_job__subscribers")
        .order_by("finished_at")[:REDRIVE_BATCH_LIMIT]
    )

    redriven = 0
    for run in candidates:
        if not within_relevance_window(run.finished_at) or run.effective_muted:
            continue
        if not notifies(run, run.envelope.status):
            continue
        # Skip only when EVERY expected recipient already has a row. A partial fan-out — a crash between
        # the per-recipient commits — must still be re-driven for the recipients that missed out.
        expected = set(resolve_recipients(run))
        if not expected:
            continue
        # Resolve the notification key exactly as the emit did: a single-repo submit carries a
        # batch_id but is delivered per-run (the rollup needs >1 sibling). Keying it as a batch here
        # would look up a rollup that never exists and re-emit it on every tick.
        if run.batch_id is not None:
            counts = Run.objects.by_batch(run.batch_id).aggregate(
                total=Count("id"), classified=Count("id", filter=Q(envelope__isnull=False))
            )
            total = counts["total"]
            # A >1 batch rollup is only deliverable once every sibling is classified; an incomplete
            # batch is pending (the reclassify backstop will classify the stragglers), not stuck.
            if total > 1 and counts["classified"] < total:
                continue
        else:
            total = 1
        source_type, source_id, event_type = notification_source_for_run(run, total)
        if expected.issubset(_delivered(source_type, source_id, event_type)):
            continue
        _notify_classified(run, run.envelope)
        # The notifier can't tell us whether the row landed, so re-read instead. A recipient still
        # missing after the re-emit is a genuinely stuck delivery (a persistent create failure), not
        # this tick's success; only a clean fan-out is counted.
        still_missing = expected - _delivered(source_type, source_id, event_type)
        if still_missing:
            logger.error(
                "redrive: run=%s still missing notification after re-emit for recipient_pk(s)=%s",
                run.pk,
                sorted(str(pk) for pk in still_missing),
            )
        else:
            redriven += 1

    if redriven:
        logger.info("redrive_missing_notifications: re-emitted the notification for %d run(s)", redriven)


@cron("*/15 * * * *")
@task
@locked_task(key="telegram-webhook-reconcile")
def telegram_webhook_reconcile_cron_task():
    """Converge Telegram's registered webhook on the desired state.

    ``sync_telegram()`` runs only on a dashboard save, and three things break that assumption:
    an env-only instance may never save the group at all, the Site domain can change under a
    registered webhook, and Telegram silently disables a webhook that keeps failing.

    ``getWebhookInfo`` does not report whether a secret is set, so that half is checked against
    the stored value; a healthy tick is a single call. This never touches bindings.

    A reported ``last_error_message`` is never healthy, so it re-syncs rather than returning. The
    condition that needs it is a stored secret that has diverged from Telegram's registered one —
    an admin save racing this cron during first sync mints two and only one reaches the DB — after
    which every update 401s forever and the URL still matches. ``setWebhook`` is idempotent, so
    re-asserting the stored secret repairs that and every other divergence for free.

    ``locked_task`` (non-blocking) skips this tick if the prior one still holds the lock.
    """
    from notifications.telegram.client import TelegramError, TGClient
    from notifications.telegram.config import sync_telegram, webhook_url

    if not site_settings.telegram_enabled:
        return
    client = TGClient.from_site_settings()
    if client is None:
        return

    try:
        result = client.get_webhook_info().get("result")
    except TelegramError as exc:
        # Anticipated on this */15 cron: an outage logged at ERROR would mint a Sentry event
        # four times an hour for its whole duration.
        logger.warning("Telegram getWebhookInfo failed: %s", exc)
        return
    except Exception:
        logger.exception("Telegram getWebhookInfo failed unexpectedly")
        return
    info = result if isinstance(result, dict) else {}

    if last_error := info.get("last_error_message"):
        # The only visibility into a webhook dying of persistent 401s.
        logger.warning(
            "Telegram webhook reports last_error_message=%r pending_update_count=%s",
            last_error,
            info.get("pending_update_count"),
        )

    try:
        desired = webhook_url()
    except NoReverseMatch:
        # The string coupling documented in telegram/config.py broke: a code bug, not a
        # misconfiguration, and nothing else would ever report it.
        logger.exception("Telegram reconcile: the callback route no longer reverses")
        return
    except Exception as exc:  # noqa: BLE001 — a Site misconfiguration must not kill the tick
        logger.warning("Telegram reconcile could not build the webhook URL: %s", exc)
        return

    current = info.get("url") or ""
    healthy = (
        not last_error
        and current == desired
        and site_settings.telegram_bot_username
        and site_settings.telegram_webhook_secret
    )
    if healthy:
        return
    if current and current != desired:
        logger.warning(
            "Telegram webhook points at a foreign URL %r; re-registering as %r. Telegram allows one "
            "webhook per bot, so another instance sharing this token is losing its handshakes.",
            current,
            desired,
        )

    try:
        warnings = sync_telegram()
    except Exception:
        logger.exception("Telegram reconcile: sync_telegram failed")
        return
    for warning in warnings:
        logger.warning("Telegram reconcile: %s", warning)
