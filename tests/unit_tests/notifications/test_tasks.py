import logging
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from django.db import IntegrityError
from django.utils import timezone

import pytest
from notifications.choices import DeliveryStatus, EnvelopeStatus, EventType
from notifications.classification.llm import ActionableDraft, RunClassification
from notifications.exceptions import UnknownChannelError, UnrecoverableDeliveryError
from notifications.models import Notification, RunEnvelope
from notifications.policy import RECLASSIFY_MAX_AGE
from notifications.tasks import (
    MAX_RETRY_AFTER_SECONDS,
    RECLASSIFY_GRACE,
    _deliver_notification,
    classify_run_task,
    reclassify_missing_envelopes_cron_task,
    redrive_missing_notifications_cron_task,
)
from sessions.models import Run, RunStatus, Session, SessionOrigin

from accounts.models import User
from schedules.models import Frequency, Intent, ScheduledJob
from tests.unit_tests.conftest import SAMPLE_QUESTION_PAYLOAD


@pytest.mark.django_db
class TestDeliverNotification:
    def test_success_marks_sent(self, notification_with_delivery):
        _n, d = notification_with_delivery
        with patch("notifications.channels.email.EmailChannel.send"):
            _deliver_notification(d.id)

        d.refresh_from_db()
        assert d.status == DeliveryStatus.SENT
        assert d.attempts == 1
        assert d.delivered_at is not None
        assert d.last_attempted_at is not None

    def test_unrecoverable_error_marks_failed_no_retry(self, notification_with_delivery):
        _n, d = notification_with_delivery
        with patch(
            "notifications.channels.email.EmailChannel.send", side_effect=UnrecoverableDeliveryError("bad address")
        ):
            _deliver_notification(d.id)

        d.refresh_from_db()
        assert d.status == DeliveryStatus.FAILED
        assert d.attempts == 1
        assert "bad address" in d.error_message

    def test_transient_error_within_max_attempts_stays_pending(self, notification_with_delivery):
        _n, d = notification_with_delivery
        with (
            patch("notifications.channels.email.EmailChannel.send", side_effect=ConnectionError("timeout")),
            patch("notifications.tasks.deliver_notification_task") as mock_task,
        ):
            _deliver_notification(d.id)

        d.refresh_from_db()
        assert d.status == DeliveryStatus.PENDING
        assert d.attempts == 1
        assert "timeout" in d.error_message
        mock_task.using.return_value.enqueue.assert_called_once()

    def test_transient_error_at_max_attempts_marks_failed(self, notification_with_delivery):
        _n, d = notification_with_delivery
        d.attempts = 2
        d.save()
        with patch("notifications.channels.email.EmailChannel.send", side_effect=ConnectionError("timeout")):
            _deliver_notification(d.id)

        d.refresh_from_db()
        assert d.status == DeliveryStatus.FAILED
        assert d.attempts == 3

    def test_skipped_delivery_is_not_processed(self, notification_with_delivery):
        _n, d = notification_with_delivery
        d.status = DeliveryStatus.SKIPPED
        d.save()
        with patch("notifications.channels.email.EmailChannel.send") as mock_send:
            _deliver_notification(d.id)
        mock_send.assert_not_called()

    def test_nonexistent_delivery_does_not_raise(self):
        _deliver_notification(uuid.uuid4())

    def test_unknown_channel_at_delivery_time(self, notification_with_delivery):
        _n, d = notification_with_delivery
        with patch("notifications.tasks.get_channel", side_effect=UnknownChannelError("sms")):
            _deliver_notification(d.id)

        d.refresh_from_db()
        assert d.status == DeliveryStatus.SKIPPED
        assert "sms" in d.error_message

    def _retry_run_after(self, delivery, exc) -> float:
        """Seconds from now until the retry the ladder scheduled for ``exc``."""
        with (
            patch("notifications.channels.email.EmailChannel.send", side_effect=exc),
            patch("notifications.tasks.deliver_notification_task") as mock_task,
        ):
            _deliver_notification(delivery.id)
        run_after = mock_task.using.call_args.kwargs["run_after"]
        return (run_after - timezone.now()).total_seconds()

    def test_a_retry_after_longer_than_the_ladder_delays_the_retry(self, notification_with_delivery):
        _n, d = notification_with_delivery
        exc = ConnectionError("flood wait")
        exc.retry_after = 300
        assert self._retry_run_after(d, exc) == pytest.approx(300, abs=5)

    def test_a_retry_after_shorter_than_the_ladder_keeps_the_ladder(self, notification_with_delivery):
        _n, d = notification_with_delivery
        exc = ConnectionError("flood wait")
        exc.retry_after = 10
        assert self._retry_run_after(d, exc) == pytest.approx(60, abs=5)

    def test_an_absurd_retry_after_is_capped(self, notification_with_delivery):
        _n, d = notification_with_delivery
        exc = ConnectionError("flood wait")
        exc.retry_after = 99_999
        assert self._retry_run_after(d, exc) == pytest.approx(MAX_RETRY_AFTER_SECONDS, abs=5)

    @pytest.mark.parametrize("value", ["300", -5, 0, True, None])
    def test_an_unusable_retry_after_keeps_the_ladder(self, notification_with_delivery, value):
        _n, d = notification_with_delivery
        exc = ConnectionError("flood wait")
        exc.retry_after = value
        assert self._retry_run_after(d, exc) == pytest.approx(60, abs=5)

    def test_reenqueue_failure_marks_failed(self, notification_with_delivery):
        _n, d = notification_with_delivery
        with (
            patch("notifications.channels.email.EmailChannel.send", side_effect=ConnectionError("timeout")),
            patch("notifications.tasks.deliver_notification_task") as mock_task,
        ):
            mock_task.using.return_value.enqueue.side_effect = RuntimeError("broker down")
            _deliver_notification(d.id)

        d.refresh_from_db()
        assert d.status == DeliveryStatus.FAILED
        assert "Re-enqueue failed" in d.error_message


_RUN_STATUS_FOR_ENVELOPE = {
    EnvelopeStatus.FAILED: RunStatus.FAILED,
    EnvelopeStatus.NEEDS_INPUT: RunStatus.WAITING_INPUT,
}


def _classified_finished_run(user, *, status=EnvelopeStatus.FAILED, origin=SessionOrigin.API_JOB, batch_id=None):
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=origin, repo_id="x/y", user=user)
    run = Run.objects.create(
        session=session,
        trigger_type=origin,
        repo_id="x/y",
        status=_RUN_STATUS_FOR_ENVELOPE.get(status, RunStatus.SUCCESSFUL),
        user=user,
        finished_at=timezone.now(),
        batch_id=batch_id,
    )
    RunEnvelope.objects.create(run=run, status=status, summary="s")
    return run


@pytest.mark.django_db
def test_redrive_delivers_missing_notification(member_user, email_binding):
    run = _classified_finished_run(member_user)  # envelope exists, but no Notification (crash-window sim)
    assert Notification.objects.filter(source_id=str(run.pk)).count() == 0
    redrive_missing_notifications_cron_task.func()
    assert Notification.objects.filter(source_type="sessions.Run", source_id=str(run.pk)).count() == 1


@pytest.mark.django_db
def test_redrive_logs_error_when_delivery_stays_missing(member_user, email_binding, caplog):
    """If a persistent create failure leaves a recipient without a row even after the re-emit, the
    re-drive surfaces it at ERROR — the operator's only signal of a genuinely stuck delivery."""
    run = _classified_finished_run(member_user)
    with (
        patch("notifications.run_notifiers.notify"),  # no-op: the re-emit writes no Notification row
        caplog.at_level(logging.ERROR, logger="daiv.notifications"),
    ):
        redrive_missing_notifications_cron_task.func()
    assert Notification.objects.filter(source_id=str(run.pk)).count() == 0
    assert any("still missing notification after re-emit" in rec.message for rec in caplog.records)


@pytest.mark.django_db
def test_redrive_second_pass_does_not_duplicate(member_user, email_binding):
    run = _classified_finished_run(member_user)
    redrive_missing_notifications_cron_task.func()
    redrive_missing_notifications_cron_task.func()
    assert Notification.objects.filter(source_type="sessions.Run", source_id=str(run.pk)).count() == 1


@pytest.mark.django_db
def test_redrive_skips_all_clear_and_out_of_window(member_user, email_binding):
    _classified_finished_run(member_user, status=EnvelopeStatus.ALL_CLEAR)  # not notify-worthy
    stale = _classified_finished_run(member_user)
    Run.objects.filter(pk=stale.pk).update(finished_at=timezone.now() - RECLASSIFY_MAX_AGE - timedelta(hours=2))
    redrive_missing_notifications_cron_task.func()
    assert Notification.objects.count() == 0


@pytest.mark.django_db
def test_redrive_skips_muted_run(member_user, email_binding):
    run = _classified_finished_run(member_user)
    Run.objects.filter(pk=run.pk).update(muted=True)  # run-level mute wins in effective_muted
    redrive_missing_notifications_cron_task.func()
    assert Notification.objects.filter(source_id=str(run.pk)).count() == 0


@pytest.mark.django_db
def test_redrive_fills_missing_recipient_after_partial_fanout(member_user, run_schedule, email_binding):
    """A crash between the per-recipient commits leaves some recipients without a row. The re-drive
    must re-emit for the missing recipients — not skip the whole run because one recipient's row exists."""
    sub = User.objects.create_user(username="redrive_sub", email="redrive_sub@test.com", password="x")  # noqa: S106
    run_schedule.subscribers.add(sub)
    session = Session.objects.create(
        thread_id=str(uuid.uuid4()),
        origin=SessionOrigin.SCHEDULE,
        repo_id="x/y",
        user=member_user,
        scheduled_job=run_schedule,
    )
    run = Run.objects.create(
        session=session,
        trigger_type=SessionOrigin.SCHEDULE,
        repo_id="x/y",
        status=RunStatus.FAILED,
        user=member_user,
        finished_at=timezone.now(),
    )
    RunEnvelope.objects.create(run=run, status=EnvelopeStatus.FAILED, summary="s")
    # Only the owner's row was committed before the simulated crash; the subscriber is missing.
    Notification.objects.create(
        recipient=member_user,
        event_type=EventType.SCHEDULE_FINISHED,
        source_type="sessions.Run",
        source_id=str(run.pk),
        subject="s",
        body="b",
    )

    redrive_missing_notifications_cron_task.func()

    per_run = Notification.objects.filter(source_type="sessions.Run", source_id=str(run.pk))
    assert per_run.filter(recipient=member_user).count() == 1  # not duplicated
    assert per_run.filter(recipient=sub).count() == 1  # backfilled


@pytest.mark.django_db
def test_redrive_batch_delivers_single_rollup(member_user, email_binding):
    batch_id = uuid.uuid4()
    for _ in range(2):
        session = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.API_JOB, repo_id="x/y", user=member_user
        )
        run = Run.objects.create(
            session=session,
            trigger_type=SessionOrigin.API_JOB,
            repo_id="x/y",
            status=RunStatus.FAILED,
            user=member_user,
            finished_at=timezone.now(),
            batch_id=batch_id,
        )
        RunEnvelope.objects.create(run=run, status=EnvelopeStatus.FAILED, summary="s")

    redrive_missing_notifications_cron_task.func()

    rollups = Notification.objects.filter(
        source_type="sessions.Batch", source_id=str(batch_id), event_type=EventType.JOB_BATCH_FINISHED
    )
    assert rollups.count() == 1


@pytest.mark.django_db
def test_redrive_skips_single_repo_batch_run_already_delivered(member_user, email_binding):
    """A single-repo submit carries a batch_id but is delivered per-run (a rollup needs >1 sibling).
    The re-drive must key it per-run and recognise the delivered row — otherwise it looks up a phantom
    sessions.Batch rollup, never matches, and re-emits the notification on every tick for 24h."""
    run = _classified_finished_run(member_user)
    Run.objects.filter(pk=run.pk).update(batch_id=uuid.uuid4())  # single-repo "batch" (total == 1)

    redrive_missing_notifications_cron_task.func()  # first pass delivers the per-run notification
    per_run = Notification.objects.filter(source_type="sessions.Run", source_id=str(run.pk))
    assert per_run.count() == 1

    # Second pass must recognise the per-run row as delivered and not re-emit.
    with patch("notifications.tasks.emit_run_notification") as notify_mock:
        redrive_missing_notifications_cron_task.func()
    notify_mock.assert_not_called()
    assert per_run.count() == 1


@pytest.mark.django_db
def test_redrive_skips_batch_not_yet_fully_classified(member_user, email_binding):
    """A >1 batch rollup is only deliverable once every sibling is classified. A candidate sibling of
    a batch with an unclassified straggler must be left for the reclassify backstop, not re-emitted
    (which would produce nothing and, with the delivery re-check, log a spurious 'still missing')."""
    batch_id = uuid.uuid4()
    classified = _classified_finished_run(member_user)
    Run.objects.filter(pk=classified.pk).update(batch_id=batch_id)
    straggler_session = Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.API_JOB, repo_id="x/y", user=member_user
    )
    Run.objects.create(  # sibling with no envelope yet
        session=straggler_session,
        trigger_type=SessionOrigin.API_JOB,
        repo_id="x/y",
        status=RunStatus.SUCCESSFUL,
        user=member_user,
        finished_at=timezone.now(),
        batch_id=batch_id,
    )

    with patch("notifications.tasks.emit_run_notification") as notify_mock:
        redrive_missing_notifications_cron_task.func()
    notify_mock.assert_not_called()
    assert Notification.objects.filter(source_id=str(batch_id)).count() == 0


@pytest.mark.django_db
def test_redrive_delivers_a_missing_prompt_driven_question(member_user, email_binding):
    run = _classified_finished_run(member_user, status=EnvelopeStatus.NEEDS_INPUT)
    redrive_missing_notifications_cron_task.func()
    assert Notification.objects.filter(source_type="sessions.Run", source_id=str(run.pk)).count() == 1


@pytest.mark.django_db
def test_redrive_leaves_webhook_question_runs_alone(member_user, email_binding, caplog):
    """Re-emitting would be skipped by the emit and then logged as a stuck delivery every tick."""
    _classified_finished_run(member_user, status=EnvelopeStatus.NEEDS_INPUT, origin=SessionOrigin.ISSUE_WEBHOOK)

    with (
        patch("notifications.tasks.emit_run_notification") as notify_mock,
        caplog.at_level(logging.ERROR, logger="daiv.notifications"),
    ):
        redrive_missing_notifications_cron_task.func()
    notify_mock.assert_not_called()
    assert "still missing" not in caplog.text


@pytest.mark.django_db
def test_redrive_delivers_the_rollup_of_a_batch_with_a_question_sibling(member_user, email_binding):
    batch_id = uuid.uuid4()
    for status in (EnvelopeStatus.FAILED, EnvelopeStatus.NEEDS_INPUT):
        _classified_finished_run(member_user, status=status, batch_id=batch_id)

    redrive_missing_notifications_cron_task.func()

    rollup = Notification.objects.get(source_type="sessions.Batch", source_id=str(batch_id))
    assert rollup.context["needs_input_count"] == 1


def _stranded_run(
    *, status=RunStatus.SUCCESSFUL, trigger_type=SessionOrigin.SCHEDULE, finished_age=None, classify_eligible=True
) -> Run:
    """A terminal Run whose finished_at is pushed past the grace window (and inside the max-age floor)."""
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.SCHEDULE, repo_id="group/repo")
    run = Run.objects.create(
        session=session,
        trigger_type=trigger_type,
        repo_id="group/repo",
        status=status,
        classify_eligible=classify_eligible,
    )
    finished = timezone.now() - (finished_age if finished_age is not None else RECLASSIFY_GRACE + timedelta(minutes=1))
    Run.objects.filter(pk=run.pk).update(finished_at=finished)
    run.refresh_from_db()
    return run


@pytest.mark.django_db
def test_reclassify_reenqueues_stranded_terminal_runs_across_origins():
    stranded = [
        _stranded_run(status=RunStatus.SUCCESSFUL, trigger_type=SessionOrigin.SCHEDULE),
        _stranded_run(status=RunStatus.FAILED, trigger_type=SessionOrigin.MR_WEBHOOK),
        _stranded_run(status=RunStatus.SUCCESSFUL, trigger_type=SessionOrigin.API_JOB),
    ]
    with patch("notifications.tasks.classify_run_task") as task:
        reclassify_missing_envelopes_cron_task.func()
    enqueued = {call.args[0] for call in task.enqueue.call_args_list}
    assert enqueued == {str(r.pk) for r in stranded}


@pytest.mark.django_db
def test_reclassify_skips_chat_classified_nonterminal_and_out_of_window():
    classified = _stranded_run()
    RunEnvelope.objects.create(run=classified, status=EnvelopeStatus.ALL_CLEAR)  # already has an envelope
    _stranded_run(status=RunStatus.RUNNING)  # non-terminal
    _stranded_run(trigger_type=SessionOrigin.CHAT)  # chat is never classified
    _stranded_run(finished_age=timedelta(minutes=1))  # inside the grace window
    _stranded_run(finished_age=RECLASSIFY_MAX_AGE + timedelta(hours=1))  # older than the recency floor
    with patch("notifications.tasks.classify_run_task") as task:
        reclassify_missing_envelopes_cron_task.func()
    task.enqueue.assert_not_called()


@pytest.mark.django_db
def test_reclassify_keys_on_finished_at_not_created_at():
    # created_at old but finished_at recent → still re-targeted (batch siblings can finish long after creation).
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.SCHEDULE, repo_id="group/repo")
    run = Run.objects.create(
        session=session, trigger_type=SessionOrigin.SCHEDULE, repo_id="group/repo", status=RunStatus.SUCCESSFUL
    )
    Run.objects.filter(pk=run.pk).update(
        created_at=timezone.now() - (RECLASSIFY_MAX_AGE + timedelta(days=2)),
        finished_at=timezone.now() - (RECLASSIFY_GRACE + timedelta(minutes=1)),
    )
    with patch("notifications.tasks.classify_run_task") as task:
        reclassify_missing_envelopes_cron_task.func()
    assert {call.args[0] for call in task.enqueue.call_args_list} == {str(run.pk)}


@pytest.mark.django_db
def test_reclassify_skips_ineligible_runs():
    """Pre-deploy runs (classify_eligible=False) are out of scope; eligible siblings are still re-enqueued."""
    eligible = _stranded_run(classify_eligible=True)
    _stranded_run(classify_eligible=False)  # pre-deploy backlog: must be skipped
    with patch("notifications.tasks.classify_run_task") as task:
        reclassify_missing_envelopes_cron_task.func()
    assert {call.args[0] for call in task.enqueue.call_args_list} == {str(eligible.pk)}


@pytest.mark.django_db
def test_reclassify_reenqueues_stranded_waiting_input_runs():
    stranded = [_stranded_run(status=RunStatus.SUCCESSFUL), _stranded_run(status=RunStatus.WAITING_INPUT)]
    with patch("notifications.tasks.classify_run_task") as task:
        reclassify_missing_envelopes_cron_task.func()
    assert {call.args[0] for call in task.enqueue.call_args_list} == {str(r.pk) for r in stranded}


def _llm_returning(classification: RunClassification) -> MagicMock:
    """A ``_build_structured_llm`` stand-in whose chain returns ``classification`` from ainvoke."""
    llm = MagicMock()
    llm.with_config.return_value.ainvoke = AsyncMock(return_value=classification)
    return llm


async def _make_scheduled_run(
    *,
    intent: str = Intent.WATCH_FIND,
    status: str = RunStatus.SUCCESSFUL,
    response_text: str = "The run finished.",
    error_message: str = "",
    with_schedule: bool = True,
) -> Run:
    """Build a ``ScheduledJob(intent=…) -> Session(scheduled_job=…) -> Run(session=…)`` chain.

    ``response_text`` is seeded via ``result_summary`` (the read-only ``Run.response_text``
    property falls back to it when no ``task_result`` is linked).
    """
    schedule = None
    if with_schedule:
        owner = await User.objects.acreate(username=f"owner-{uuid.uuid4()}", email=f"{uuid.uuid4()}@t.com")
        schedule = await ScheduledJob.objects.acreate(
            user=owner,
            name="nightly",
            prompt="check the repo",
            repos=[{"repo_id": "group/project", "ref": ""}],
            frequency=Frequency.DAILY,
            intent=intent,
        )
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.SCHEDULE, repo_id="group/project", scheduled_job=schedule
    )
    return await Run.objects.acreate(
        session=session,
        trigger_type=SessionOrigin.SCHEDULE,
        repo_id="group/project",
        status=status,
        result_summary=response_text,
        error_message=error_message,
    )


@pytest.mark.django_db(transaction=True)
async def test_ac2_well_formed_envelope_for_watch_find():
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND, response_text="Found a couple of problems.")
    classification = RunClassification(
        status="found-issues",
        summary="Two issues found in the auth module.",
        actionable=[
            ActionableDraft(kind="bug", label="Null deref in login", ref="auth/login.py"),
            ActionableDraft(kind="todo", label="Missing test", ref="auth/tests.py"),
        ],
    )

    with patch("notifications.classification.llm._build_structured_llm", return_value=_llm_returning(classification)):
        await classify_run_task.func(str(run.pk))

    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.status == EnvelopeStatus.FOUND_ISSUES
    assert envelope.count == len(envelope.actionable) == 2
    assert envelope.summary == "Two issues found in the auth module."
    # Every item is contract-shaped: id + schema_version stamped by build_actionable_item.
    for item in envelope.actionable:
        assert set(item) >= {"id", "kind", "label", "ref", "schema_version"}
        assert isinstance(item["id"], str)
        assert item["schema_version"] == 1
    # Prose is retained and reachable (the classifier never touches the run's own result).
    reloaded = await Run.objects.aget(pk=run.pk)
    assert reloaded.response_text == "Found a couple of problems."


@pytest.mark.django_db(transaction=True)
async def test_ac3_report_intent_coerces_found_issues_to_needs_attention():
    run = await _make_scheduled_run(intent=Intent.REPORT)
    classification = RunClassification(
        status="found-issues",
        summary="Weekly summary of activity.",
        actionable=[ActionableDraft(kind="bug", label="x", ref="y")],
    )

    with patch("notifications.classification.llm._build_structured_llm", return_value=_llm_returning(classification)):
        await classify_run_task.func(str(run.pk))

    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.status == EnvelopeStatus.NEEDS_ATTENTION
    assert envelope.actionable == []
    assert envelope.count == 0


@pytest.mark.django_db(transaction=True)
async def test_ac3_report_intent_passes_all_clear_through_with_no_findings():
    run = await _make_scheduled_run(intent=Intent.REPORT)
    classification = RunClassification(
        status="all-clear", summary="Nothing notable.", actionable=[ActionableDraft(kind="bug", label="x", ref="y")]
    )

    with patch("notifications.classification.llm._build_structured_llm", return_value=_llm_returning(classification)):
        await classify_run_task.func(str(run.pk))

    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.status == EnvelopeStatus.ALL_CLEAR
    assert envelope.actionable == []
    assert envelope.count == 0


@pytest.mark.django_db(transaction=True)
async def test_ac4_found_issues_with_empty_list_coerced_to_all_clear():
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND)
    classification = RunClassification(status="found-issues", summary="Odd but empty.", actionable=[])

    with patch("notifications.classification.llm._build_structured_llm", return_value=_llm_returning(classification)):
        await classify_run_task.func(str(run.pk))

    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.status == EnvelopeStatus.ALL_CLEAR
    assert envelope.actionable == []
    assert envelope.count == 0


@pytest.mark.django_db(transaction=True)
async def test_ac5_failed_run_is_failed_status_no_llm_call():
    # Non-empty prose on purpose: proves the FAILED gate wins over the classification path even when
    # there *is* text to classify (not merely over the empty-prose short-circuit).
    run = await _make_scheduled_run(
        status=RunStatus.FAILED,
        response_text="There is prose here that must NOT be classified.",
        error_message="Traceback: boom\nsecond line",
    )

    with patch("notifications.classification.llm._build_structured_llm") as build:
        await classify_run_task.func(str(run.pk))

    build.assert_not_called()  # no LLM/method call for a failed run
    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.status == EnvelopeStatus.FAILED
    assert envelope.actionable == []
    assert envelope.count == 0
    # Summary is the first non-empty line of error_message, stated plainly (never a finding).
    assert envelope.summary == "Traceback: boom"


@pytest.mark.django_db(transaction=True)
async def test_ac5_failed_run_without_error_message_uses_generic_gloss():
    run = await _make_scheduled_run(status=RunStatus.FAILED, response_text="", error_message="")

    with patch("notifications.classification.llm._build_structured_llm") as build:
        await classify_run_task.func(str(run.pk))

    build.assert_not_called()
    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.status == EnvelopeStatus.FAILED
    assert envelope.summary == "Run failed."


@pytest.mark.django_db(transaction=True)
async def test_ac6_idempotent_writes_exactly_one_envelope():
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND)
    classification = RunClassification(status="all-clear", summary="All good.", actionable=[])

    with patch(
        "notifications.classification.llm._build_structured_llm", return_value=_llm_returning(classification)
    ) as build:
        await classify_run_task.func(str(run.pk))
        await classify_run_task.func(str(run.pk))  # second call must no-op via the aexists guard

    assert await RunEnvelope.objects.filter(run=run).acount() == 1
    # Second call short-circuits before the method is invoked again.
    build.assert_called_once()


@pytest.mark.django_db(transaction=True)
async def test_ac6_pending_run_has_no_envelope():
    run = await _make_scheduled_run()
    # Task not run yet → envelope is absent ("classifying…").
    assert not await RunEnvelope.objects.filter(run=run).aexists()


@pytest.mark.django_db(transaction=True)
async def test_schedule_deleted_defaults_intent_to_watch_find():
    # A SCHEDULE-triggered run whose schedule was deleted (scheduled_job is None) still classifies;
    # intent defaults to WATCH_FIND, so found-issues + items is preserved as a finding.
    run = await _make_scheduled_run(with_schedule=False)
    classification = RunClassification(
        status="found-issues", summary="One problem.", actionable=[ActionableDraft(kind="bug", label="x", ref="y")]
    )

    with patch("notifications.classification.llm._build_structured_llm", return_value=_llm_returning(classification)):
        await classify_run_task.func(str(run.pk))

    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.status == EnvelopeStatus.FOUND_ISSUES
    assert envelope.count == 1


@pytest.mark.django_db(transaction=True)
async def test_no_model_configured_leaves_run_unclassified(site_settings_override):
    run = await _make_scheduled_run()
    site_settings_override(run_classifier_model_name="", run_classifier_fallback_model_name="")

    with patch("notifications.classification.llm._build_structured_llm") as build:
        await classify_run_task.func(str(run.pk))  # must not raise

    build.assert_not_called()
    assert not await RunEnvelope.objects.filter(run=run).aexists()


@pytest.mark.django_db(transaction=True)
async def test_missing_run_is_a_clean_skip():
    with patch("notifications.classification.llm._build_structured_llm") as build:
        await classify_run_task.func(str(uuid.uuid4()))  # must not raise

    build.assert_not_called()
    assert await RunEnvelope.objects.acount() == 0


@pytest.mark.django_db(transaction=True)
async def test_method_failure_propagates_without_partial_envelope():
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND)
    failing = MagicMock()
    failing.with_config.return_value.ainvoke = AsyncMock(side_effect=RuntimeError("upstream 500"))

    with (
        patch("notifications.classification.llm._build_structured_llm", return_value=failing),
        pytest.raises(RuntimeError),
    ):
        await classify_run_task.func(str(run.pk))

    assert not await RunEnvelope.objects.filter(run=run).aexists()


@pytest.mark.django_db(transaction=True)
async def test_non_report_all_clear_with_items_is_emptied():
    # Off-contract draft: a non-report run returns ``all-clear`` yet carries items. Only
    # ``found-issues`` may carry items, so the task empties them — status and actionable can never
    # disagree (the reverse of the AC4 invariant).
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND)
    classification = RunClassification(
        status="all-clear", summary="Nothing to do.", actionable=[ActionableDraft(kind="bug", label="x", ref="y")]
    )

    with patch("notifications.classification.llm._build_structured_llm", return_value=_llm_returning(classification)):
        await classify_run_task.func(str(run.pk))

    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.status == EnvelopeStatus.ALL_CLEAR
    assert envelope.actionable == []
    assert envelope.count == 0


@pytest.mark.django_db(transaction=True)
async def test_non_report_needs_attention_with_items_is_emptied():
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND)
    classification = RunClassification(
        status="needs-attention", summary="Take a look.", actionable=[ActionableDraft(kind="bug", label="x", ref="y")]
    )

    with patch("notifications.classification.llm._build_structured_llm", return_value=_llm_returning(classification)):
        await classify_run_task.func(str(run.pk))

    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.status == EnvelopeStatus.NEEDS_ATTENTION
    assert envelope.actionable == []
    assert envelope.count == 0


@pytest.mark.django_db(transaction=True)
async def test_successful_run_with_empty_prose_is_all_clear_without_llm_call():
    # A SUCCESSFUL run with empty prose (code-only run) has nothing to classify: write ``all-clear``
    # directly, never calling the method with an empty prompt.
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND, response_text="")

    with patch("notifications.classification.llm._build_structured_llm") as build:
        await classify_run_task.func(str(run.pk))

    build.assert_not_called()
    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.status == EnvelopeStatus.ALL_CLEAR
    assert envelope.actionable == []
    assert envelope.count == 0
    assert envelope.summary == ""


@pytest.mark.django_db(transaction=True)
async def test_forwards_run_prose_and_model_names_to_method(site_settings_override):
    # Seam assertion: the run's prose and the primary-before-fallback model tuple (empties filtered)
    # actually reach the classification method — not just that the output happens to be right.
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND, response_text="classify this exact prose")
    llm = _llm_returning(RunClassification(status="all-clear", summary="ok", actionable=[]))
    site_settings_override(run_classifier_model_name="primary", run_classifier_fallback_model_name="fallback")

    with patch("notifications.classification.llm._build_structured_llm", return_value=llm) as build:
        await classify_run_task.func(str(run.pk))

    build.assert_called_once_with(RunClassification, ("primary", "fallback"))
    messages = llm.with_config.return_value.ainvoke.call_args.args[0]
    assert messages[1].content == "classify this exact prose"


@pytest.mark.django_db(transaction=True)
async def test_single_configured_model_forwards_one_tuple(site_settings_override):
    # The empty-fallback is filtered out, so the method receives a 1-tuple (not an empty or 2-tuple).
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND)
    llm = _llm_returning(RunClassification(status="all-clear", summary="ok", actionable=[]))
    site_settings_override(run_classifier_model_name="only", run_classifier_fallback_model_name="")

    with patch("notifications.classification.llm._build_structured_llm", return_value=llm) as build:
        await classify_run_task.func(str(run.pk))

    build.assert_called_once_with(RunClassification, ("only",))


@pytest.mark.django_db(transaction=True)
async def test_failed_run_summary_skips_leading_blank_lines():
    # First *non-empty* line, stripped — a regression to ``splitlines()[0]`` would yield "" here and
    # fall back to the generic gloss.
    run = await _make_scheduled_run(
        status=RunStatus.FAILED, response_text="", error_message="\n\n   Real error here   \n"
    )

    with patch("notifications.classification.llm._build_structured_llm") as build:
        await classify_run_task.func(str(run.pk))

    build.assert_not_called()
    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.summary == "Real error here"


@pytest.mark.django_db(transaction=True)
async def test_unexpected_integrity_error_propagates_and_writes_nothing():
    # A genuine IntegrityError (not the documented OneToOne race) must surface as a FAILED task rather
    # than being swallowed as a no-op — otherwise the run is left unclassified with no signal.
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND)
    classification = RunClassification(status="all-clear", summary="ok", actionable=[])

    with (
        patch("notifications.classification.llm._build_structured_llm", return_value=_llm_returning(classification)),
        patch.object(RunEnvelope.objects, "acreate", AsyncMock(side_effect=IntegrityError("boom"))),
        pytest.raises(IntegrityError),
    ):
        await classify_run_task.func(str(run.pk))

    # No envelope exists → the except re-checks aexists (False) and re-raises.
    assert await RunEnvelope.objects.filter(run=run).acount() == 0


@pytest.mark.django_db(transaction=True)
async def test_persist_noops_when_race_winner_already_wrote_envelope():
    # The documented OneToOne race: the aexists guard passed, but a concurrent task wrote the envelope
    # before our acreate. The IntegrityError is caught, the re-check finds the winner's row, and we
    # no-op cleanly (no raise, exactly one envelope — the winner's).
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND)
    classification = RunClassification(status="all-clear", summary="loser", actionable=[])

    async def _winner_then_raise(**kwargs):
        winner = RunEnvelope(run=run, status=EnvelopeStatus.ALL_CLEAR, count=0, summary="winner", actionable=[])
        await winner.asave()
        raise IntegrityError("duplicate key value violates unique constraint")

    with (
        patch("notifications.classification.llm._build_structured_llm", return_value=_llm_returning(classification)),
        patch.object(RunEnvelope.objects, "acreate", AsyncMock(side_effect=_winner_then_raise)),
    ):
        await classify_run_task.func(str(run.pk))  # must not raise

    assert await RunEnvelope.objects.filter(run=run).acount() == 1
    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.summary == "winner"


@pytest.mark.django_db(transaction=True)
async def test_non_successful_run_is_skipped_without_llm_call():
    # Defensive guard (Epic 1 review): only a SUCCESSFUL run reaches classification. A non-terminal
    # run (e.g. a manual re-enqueue while still RUNNING, or a future third terminal RunStatus) must be
    # skipped, never dressed as ``all-clear``/``found-issues``.
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND, status=RunStatus.RUNNING)

    with patch("notifications.classification.llm._build_structured_llm") as build:
        await classify_run_task.func(str(run.pk))

    build.assert_not_called()
    assert not await RunEnvelope.objects.filter(run=run).aexists()


@pytest.mark.django_db(transaction=True)
async def test_notifies_once_on_success(member_user):
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND, response_text="")  # empty prose → all-clear fast path
    received = []

    def _spy(run, envelope):
        # Sync ORM inside the notifier proves the sync_to_async bridge works (no SynchronousOnlyOperation).
        assert RunEnvelope.objects.filter(pk=envelope.pk).exists()
        received.append((run.pk, envelope.status))

    with patch("notifications.tasks.emit_run_notification", side_effect=_spy):
        await classify_run_task.func(str(run.pk))

    assert received == [(run.pk, EnvelopeStatus.ALL_CLEAR)]


@pytest.mark.django_db(transaction=True)
async def test_a_notifier_failure_keeps_the_envelope_and_does_not_fail_the_task(caplog):
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND, response_text="")

    with (
        patch("notifications.tasks.emit_run_notification", side_effect=RuntimeError("smtp down")),
        caplog.at_level(logging.ERROR, logger="daiv.notifications"),
    ):
        await classify_run_task.func(str(run.pk))  # must not raise

    assert await RunEnvelope.objects.filter(run=run, status=EnvelopeStatus.ALL_CLEAR).aexists()
    assert f"Notifying for classified run={run.pk} failed" in caplog.text


@pytest.mark.django_db(transaction=True)
async def test_raced_loser_does_not_notify():
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND)
    classification = RunClassification(status="all-clear", summary="loser", actionable=[])

    async def _winner_then_raise(**kwargs):
        winner = RunEnvelope(run=run, status=EnvelopeStatus.ALL_CLEAR, count=0, summary="winner", actionable=[])
        await winner.asave()
        raise IntegrityError("duplicate key value violates unique constraint")

    with (
        patch("notifications.classification.llm._build_structured_llm", return_value=_llm_returning(classification)),
        patch.object(RunEnvelope.objects, "acreate", AsyncMock(side_effect=_winner_then_raise)),
        patch("notifications.tasks.emit_run_notification") as notify,
    ):
        await classify_run_task.func(str(run.pk))  # must not raise

    notify.assert_not_called()  # the raced loser never notifies
    assert await RunEnvelope.objects.filter(run=run).acount() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("response_text", "model"),
    [("There is prose here.", "openrouter:primary"), ("", "openrouter:primary"), ("There is prose here.", "")],
    ids=["prose", "empty-prose", "no-classifier-model"],
)
async def test_waiting_input_run_is_needs_input_with_the_question_lines_and_no_llm_call(
    site_settings_override, response_text, model
):
    run = await _make_scheduled_run(status=RunStatus.WAITING_INPUT, response_text=response_text)
    await Run.objects.filter(pk=run.pk).aupdate(question=SAMPLE_QUESTION_PAYLOAD)
    site_settings_override(run_classifier_model_name=model, run_classifier_fallback_model_name="")

    with patch("notifications.classification.llm._build_structured_llm") as build:
        await classify_run_task.func(str(run.pk))

    build.assert_not_called()
    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.status == EnvelopeStatus.NEEDS_INPUT
    assert envelope.actionable == []
    assert envelope.count == 0
    assert envelope.summary == "Database: Which database engine should the project move to?"


@pytest.mark.django_db(transaction=True)
async def test_waiting_input_run_without_a_stored_question_still_gets_an_envelope(caplog):
    run = await _make_scheduled_run(status=RunStatus.WAITING_INPUT)

    with (
        patch("notifications.classification.llm._build_structured_llm") as build,
        caplog.at_level("ERROR", "daiv.notifications"),
    ):
        await classify_run_task.func(str(run.pk))

    build.assert_not_called()
    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.status == EnvelopeStatus.NEEDS_INPUT
    assert envelope.summary == "Waiting for your answer."
    assert "has no question to show" in caplog.text
