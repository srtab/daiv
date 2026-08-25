"""Tests for notifications receivers wired to sessions.signals.run_classified (and run_finished for memory)."""

import logging
import uuid
from unittest.mock import patch

from django.utils import timezone

import pytest
from notifications.choices import ChannelType
from notifications.models import Notification, NotificationDelivery
from notifications.policy import notify_worthy_statuses
from notifications.run_notifiers import ACTIONABLE_CONTEXT_LIMIT, NOTABLE_RUNS_CONTEXT_LIMIT
from sessions.models import EnvelopeStatus, Run, RunEnvelope, RunStatus, Session, SessionOrigin
from sessions.signals import run_classified, run_finished

from accounts.models import User


@pytest.fixture(autouse=True)
def _isolate_memory_extraction():
    """``run_finished`` also drives memory extraction. Stub the task so these
    notification tests stay hermetic and don't contend on the SQLite write lock via
    the synchronous task backend. Tests that assert on the memory task patch it
    themselves (the inner patch wins for their duration)."""
    with patch("memory.signals.extract_observations_task"):
        yield


def _session(*, origin=SessionOrigin.API_JOB, thread_id="thread-run-1", repo_id="x/y", **kwargs):
    return Session.objects.create(thread_id=thread_id, origin=origin, repo_id=repo_id, **kwargs)


def _run(session, *, trigger_type=SessionOrigin.API_JOB, status=RunStatus.SUCCESSFUL, repo_id="x/y", **kwargs):
    return Run.objects.create(session=session, trigger_type=trigger_type, status=status, repo_id=repo_id, **kwargs)


def _classified_run(
    session,
    *,
    status=EnvelopeStatus.FOUND_ISSUES,
    count=1,
    summary="s",
    trigger_type=SessionOrigin.API_JOB,
    muted=None,
    user=None,
    finished_at=None,
    **kwargs,
):
    run = _run(
        session,
        trigger_type=trigger_type,
        status=RunStatus.SUCCESSFUL,
        user=user,
        muted=muted,
        finished_at=finished_at or timezone.now(),
        **kwargs,
    )
    actionable = (
        [{"id": "0", "kind": "bug", "label": "x", "ref": "y", "schema_version": 1}]
        if status == EnvelopeStatus.FOUND_ISSUES and count
        else []
    )
    envelope = RunEnvelope.objects.create(run=run, status=status, summary=summary, actionable=actionable)
    return run, envelope


def _make_run_batch(user, *, statuses, repos=None, batch_id=None, scheduled_job=None):
    """Create one Run per status, each in its own Session, all sharing one ``batch_id``.

    Mirrors the old activity-batch helper. Siblings live in separate sessions so the
    one-active-per-session constraint never fires (batch grouping is by ``batch_id``,
    which spans sessions).
    """
    bid = batch_id or uuid.uuid4()
    repos = repos or [f"acme/repo{i}" for i in range(len(statuses))]
    trigger = SessionOrigin.SCHEDULE if scheduled_job else SessionOrigin.API_JOB
    runs = []
    for i, status in enumerate(statuses):
        session = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=trigger, repo_id=repos[i], user=user, scheduled_job=scheduled_job
        )
        runs.append(
            Run.objects.create(
                session=session, trigger_type=trigger, repo_id=repos[i], status=status, user=user, batch_id=bid
            )
        )
    return runs


@pytest.mark.django_db
class TestMemorySkipChatRuns:
    """capture_run_observations must ignore CHAT-triggered runs."""

    def test_memory_skips_chat_runs(self):
        session = _session(origin=SessionOrigin.CHAT, thread_id="chat-thread")
        run = _run(session, trigger_type=SessionOrigin.CHAT)
        with patch("memory.signals.extract_observations_task") as task_mock:
            run_finished.send(sender=Run, run=run)
        task_mock.enqueue.assert_not_called()

    def test_memory_processes_api_job_runs(self):
        session = _session()
        run = _run(session)
        with patch("memory.signals.extract_observations_task") as task_mock:
            run_finished.send(sender=Run, run=run)
        task_mock.enqueue.assert_called_once_with(str(run.pk))


def _classify(run, status, *, count=0, summary=""):
    actionable = (
        [{"id": "0", "kind": "bug", "label": "x", "ref": "y", "schema_version": 1}]
        if status == EnvelopeStatus.FOUND_ISSUES
        else []
    )
    return RunEnvelope.objects.create(run=run, status=status, summary=summary, actionable=actionable)


@pytest.mark.django_db
class TestRunBatchRollup:
    def _finish(self, run):
        run.finished_at = timezone.now()
        run.save(update_fields=["finished_at"])

    def test_rollup_fires_only_after_all_siblings_classified(self, member_user, email_binding):
        a, b = _make_run_batch(member_user, statuses=[RunStatus.SUCCESSFUL, RunStatus.SUCCESSFUL])
        for r in (a, b):
            self._finish(r)
        env_a = _classify(a, EnvelopeStatus.FOUND_ISSUES)
        run_classified.send(sender=Run, run=a, envelope=env_a)
        assert Notification.objects.filter(event_type="job_batch.finished").count() == 0  # b not classified yet

        env_b = _classify(b, EnvelopeStatus.ALL_CLEAR)
        run_classified.send(sender=Run, run=b, envelope=env_b)
        rollups = Notification.objects.filter(recipient=member_user, event_type="job_batch.finished")
        assert rollups.count() == 1
        rollup = rollups.get()
        assert rollup.context["notable_count"] == 1
        assert rollup.context["total"] == 2

    def test_rollup_breakdown_counts_each_envelope_status(self, member_user, email_binding):
        """The rollup decomposes the batch per envelope status. A one-of-each mix catches a swapped
        Count(filter=...) that the notable-only assertion above would miss."""
        runs = _make_run_batch(member_user, statuses=[RunStatus.SUCCESSFUL] * 4)
        statuses = [
            EnvelopeStatus.FOUND_ISSUES,
            EnvelopeStatus.NEEDS_ATTENTION,
            EnvelopeStatus.FAILED,
            EnvelopeStatus.ALL_CLEAR,
        ]
        for run, status in zip(runs, statuses, strict=True):
            self._finish(run)
            run_classified.send(sender=Run, run=run, envelope=_classify(run, status))

        ctx = Notification.objects.get(recipient=member_user, event_type="job_batch.finished").context
        assert ctx["found_count"] == 1
        assert ctx["needs_attention_count"] == 1
        assert ctx["failed_count"] == 1
        assert ctx["all_clear_count"] == 1
        assert ctx["notable_count"] == 3
        assert ctx["total"] == 4
        # notable (3) < total (4) → the partial "warning" tone the email pill colours amber.
        assert ctx["status_tone"] == "warning"
        assert ctx["status_label"] == "Needs attention"

    def test_rollup_tone_is_failure_when_every_run_is_notable(self, member_user, email_binding):
        """When no run is all-clear (notable == total) the tone escalates to failure (red pill),
        mirroring the RocketChat renderer's notable-vs-total thresholds."""
        runs = _make_run_batch(member_user, statuses=[RunStatus.SUCCESSFUL, RunStatus.FAILED])
        for run, status in zip(runs, [EnvelopeStatus.FOUND_ISSUES, EnvelopeStatus.FAILED], strict=True):
            self._finish(run)
            run_classified.send(sender=Run, run=run, envelope=_classify(run, status))

        ctx = Notification.objects.get(recipient=member_user, event_type="job_batch.finished").context
        assert ctx["notable_count"] == ctx["total"] == 2
        assert ctx["status_tone"] == "failure"

    def test_all_clear_batch_is_silent(self, member_user, email_binding):
        a, b = _make_run_batch(member_user, statuses=[RunStatus.SUCCESSFUL, RunStatus.SUCCESSFUL])
        for r, env_status in ((a, EnvelopeStatus.ALL_CLEAR), (b, EnvelopeStatus.ALL_CLEAR)):
            self._finish(r)
            run_classified.send(sender=Run, run=r, envelope=_classify(r, env_status))
        assert Notification.objects.filter(event_type="job_batch.finished").count() == 0

    def test_muted_batch_is_silent(self, member_user, run_schedule):
        run_schedule.muted = True
        run_schedule.save(update_fields=["muted"])
        a, b = _make_run_batch(
            member_user, statuses=[RunStatus.SUCCESSFUL, RunStatus.SUCCESSFUL], scheduled_job=run_schedule
        )
        for r in (a, b):
            self._finish(r)
            run_classified.send(sender=Run, run=r, envelope=_classify(r, EnvelopeStatus.FAILED))
        assert Notification.objects.filter(event_type="job_batch.finished").count() == 0

    def test_concurrent_last_siblings_create_one_rollup(self, member_user, caplog):
        a, b = _make_run_batch(member_user, statuses=[RunStatus.SUCCESSFUL, RunStatus.SUCCESSFUL])
        for r in (a, b):
            self._finish(r)
        _classify(a, EnvelopeStatus.FAILED)
        _classify(b, EnvelopeStatus.FAILED)
        # Both observe all-classified before either inserts; the second hits the IntegrityError path.
        run_classified.send(sender=Run, run=a, envelope=a.envelope)
        with caplog.at_level(logging.DEBUG, logger="daiv.notifications"):
            run_classified.send(sender=Run, run=b, envelope=b.envelope)
        assert Notification.objects.filter(event_type="job_batch.finished").count() == 1
        assert any("already exists" in rec.message for rec in caplog.records)

    def test_single_run_batch_falls_back_to_per_run(self, member_user, email_binding):
        (a,) = _make_run_batch(member_user, statuses=[RunStatus.SUCCESSFUL])
        self._finish(a)
        run_classified.send(sender=Run, run=a, envelope=_classify(a, EnvelopeStatus.FOUND_ISSUES))
        assert Notification.objects.filter(event_type="job.finished").count() == 1
        assert Notification.objects.filter(event_type="job_batch.finished").count() == 0

    def test_schedule_batch_fans_out_to_subscribers(self, member_user, run_schedule):
        sub = User.objects.create_user(username="batch_sub", email="batch_sub@test.com", password="x")  # noqa: S106
        run_schedule.subscribers.add(sub)
        a, b = _make_run_batch(
            member_user, statuses=[RunStatus.SUCCESSFUL, RunStatus.SUCCESSFUL], scheduled_job=run_schedule
        )
        for r in (a, b):
            self._finish(r)
            run_classified.send(sender=Run, run=r, envelope=_classify(r, EnvelopeStatus.FOUND_ISSUES))
        assert Notification.objects.filter(recipient=member_user, event_type="job_batch.finished").count() == 1
        assert Notification.objects.filter(recipient=sub, event_type="job_batch.finished").count() == 1

    def test_batch_without_recipients_logs_warning(self, caplog):
        """A completed, notable batch whose runs resolve no recipient (webhook actor with no account)
        is a dropped rollup — it logs a warning rather than vanishing silently."""
        a, b = _make_run_batch(None, statuses=[RunStatus.SUCCESSFUL, RunStatus.SUCCESSFUL])
        for r in (a, b):
            self._finish(r)
        _classify(a, EnvelopeStatus.FAILED)
        _classify(b, EnvelopeStatus.FAILED)
        run_classified.send(sender=Run, run=a, envelope=a.envelope)
        with caplog.at_level(logging.WARNING, logger="daiv.notifications"):
            run_classified.send(sender=Run, run=b, envelope=b.envelope)
        assert Notification.objects.filter(event_type="job_batch.finished").count() == 0
        assert any("no resolvable recipients" in rec.message for rec in caplog.records)

    def test_batch_duration_spans_earliest_start_to_latest_finish(self, member_user, email_binding):
        """The rollup's duration is the wall-clock span across siblings (earliest start → latest
        finish), not any single run's duration."""
        from datetime import timedelta

        a, b = _make_run_batch(member_user, statuses=[RunStatus.SUCCESSFUL, RunStatus.SUCCESSFUL])
        t0 = timezone.now() - timedelta(minutes=5)
        a.started_at, a.finished_at = t0, t0 + timedelta(seconds=30)
        a.save(update_fields=["started_at", "finished_at"])
        b.started_at, b.finished_at = t0 + timedelta(seconds=10), t0 + timedelta(seconds=90)
        b.save(update_fields=["started_at", "finished_at"])
        for r, status in ((a, EnvelopeStatus.FOUND_ISSUES), (b, EnvelopeStatus.FAILED)):
            run_classified.send(sender=Run, run=r, envelope=_classify(r, status))

        ctx = Notification.objects.get(recipient=member_user, event_type="job_batch.finished").context
        assert ctx["duration_seconds"] == 90.0  # t0 → t0+90s

    def test_webhook_batch_subject_names_repos_and_truncates(self, member_user, email_binding):
        """A webhook/API batch has no name or owner in its subject, so it names the repos, truncating
        past three with 'and N more'."""
        runs = _make_run_batch(
            member_user, statuses=[RunStatus.SUCCESSFUL] * 4, repos=["acme/a", "acme/b", "acme/c", "acme/d"]
        )
        for r in runs:
            self._finish(r)
            run_classified.send(sender=Run, run=r, envelope=_classify(r, EnvelopeStatus.FAILED))
        subject = Notification.objects.get(recipient=member_user, event_type="job_batch.finished").subject
        assert "acme/a, acme/b, acme/c and 1 more" in subject


@pytest.mark.django_db
class TestNotificationPolicy:
    def test_all_clear_writes_nothing(self, member_user):
        session = _session(user=member_user)
        run, envelope = _classified_run(session, status=EnvelopeStatus.ALL_CLEAR, count=0, user=member_user)
        run_classified.send(sender=Run, run=run, envelope=envelope)
        assert Notification.objects.filter(recipient=member_user).count() == 0

    @pytest.mark.parametrize(
        "status", [EnvelopeStatus.FOUND_ISSUES, EnvelopeStatus.NEEDS_ATTENTION, EnvelopeStatus.FAILED]
    )
    def test_notify_worthy_writes_bell_and_channels(self, member_user, email_binding, status):
        session = _session(user=member_user)
        run, envelope = _classified_run(session, status=status, user=member_user)
        run_classified.send(sender=Run, run=run, envelope=envelope)
        assert Notification.objects.filter(recipient=member_user, event_type="job.finished").count() == 1
        assert NotificationDelivery.objects.filter(channel_type=ChannelType.EMAIL).count() == 1

    def test_muted_run_is_fully_silent(self, member_user, email_binding):
        session = _session(user=member_user)
        run, envelope = _classified_run(session, status=EnvelopeStatus.FOUND_ISSUES, muted=True, user=member_user)
        run_classified.send(sender=Run, run=run, envelope=envelope)
        assert Notification.objects.filter(recipient=member_user).count() == 0

    def test_muted_schedule_suppressed_and_run_override_unmutes(self, member_user, run_schedule):
        run_schedule.muted = True
        run_schedule.save(update_fields=["muted"])
        session = _session(origin=SessionOrigin.SCHEDULE, thread_id=str(uuid.uuid4()), scheduled_job=run_schedule)
        run, envelope = _classified_run(
            session, status=EnvelopeStatus.FAILED, trigger_type=SessionOrigin.SCHEDULE, user=member_user
        )
        run_classified.send(sender=Run, run=run, envelope=envelope)
        assert Notification.objects.filter(recipient=member_user).count() == 0

        session2 = _session(origin=SessionOrigin.SCHEDULE, thread_id=str(uuid.uuid4()), scheduled_job=run_schedule)
        run2, envelope2 = _classified_run(
            session2, status=EnvelopeStatus.FAILED, trigger_type=SessionOrigin.SCHEDULE, user=member_user, muted=False
        )
        run_classified.send(sender=Run, run=run2, envelope=envelope2)
        assert Notification.objects.filter(recipient=member_user).count() == 1

    def test_stale_run_outside_window_suppressed(self, member_user, email_binding):
        from datetime import timedelta

        from sessions.tasks import RECLASSIFY_MAX_AGE

        session = _session(user=member_user)
        run, envelope = _classified_run(
            session,
            status=EnvelopeStatus.FAILED,
            user=member_user,
            finished_at=timezone.now() - RECLASSIFY_MAX_AGE - timedelta(hours=1),
        )
        run_classified.send(sender=Run, run=run, envelope=envelope)
        assert Notification.objects.filter(recipient=member_user).count() == 0

    def test_webhook_run_notifies_run_user_on_notify_worthy(self, member_user, email_binding):
        session = _session(origin=SessionOrigin.ISSUE_WEBHOOK, thread_id=str(uuid.uuid4()), user=member_user)
        run, envelope = _classified_run(
            session, status=EnvelopeStatus.FOUND_ISSUES, trigger_type=SessionOrigin.ISSUE_WEBHOOK, user=member_user
        )
        run_classified.send(sender=Run, run=run, envelope=envelope)
        assert Notification.objects.filter(recipient=member_user).count() == 1

    def test_webhook_run_without_recipient_logs_and_drops(self, caplog):
        """A webhook run whose external actor has no DAIV account resolves no recipient. Rather than
        drop it silently, the emit path logs a warning (matching the batch path) and writes nothing."""
        session = _session(origin=SessionOrigin.ISSUE_WEBHOOK, thread_id=str(uuid.uuid4()))
        run, envelope = _classified_run(
            session, status=EnvelopeStatus.FOUND_ISSUES, trigger_type=SessionOrigin.ISSUE_WEBHOOK, user=None
        )
        with caplog.at_level(logging.WARNING, logger="daiv.notifications"):
            run_classified.send(sender=Run, run=run, envelope=envelope)
        assert Notification.objects.count() == 0
        assert any("no resolvable recipient" in rec.message for rec in caplog.records)

    @pytest.mark.parametrize(
        ("status", "tone", "label"),
        [
            (EnvelopeStatus.FOUND_ISSUES, "warning", "Found issues"),
            (EnvelopeStatus.NEEDS_ATTENTION, "warning", "Needs attention"),
            (EnvelopeStatus.FAILED, "failure", "Failed"),
        ],
    )
    def test_per_run_pill_tone_follows_envelope_not_run_status(self, member_user, email_binding, status, tone, label):
        """The pill/attachment tone is driven by the envelope, never the run's own success: a
        found-issues run finishes successfully (is_successful True) yet must not render green."""
        session = _session(user=member_user)
        run, envelope = _classified_run(session, status=status, user=member_user)
        run_classified.send(sender=Run, run=run, envelope=envelope)
        ctx = Notification.objects.get(recipient=member_user, event_type="job.finished").context
        assert ctx["status_tone"] == tone
        assert ctx["status_label"] == label
        assert ctx["is_successful"] is True  # run succeeded; the tone still is not green

    def test_notification_context_carries_run_metadata(self, member_user, run_schedule):
        from decimal import Decimal

        session = _session(origin=SessionOrigin.SCHEDULE, thread_id=str(uuid.uuid4()), scheduled_job=run_schedule)
        run, envelope = _classified_run(
            session,
            status=EnvelopeStatus.FOUND_ISSUES,
            trigger_type=SessionOrigin.SCHEDULE,
            user=member_user,
            repo_id="acme/app",
            input_tokens=100,
            output_tokens=200,
            total_tokens=300,
            cost_usd=Decimal("0.05"),
        )
        run_classified.send(sender=Run, run=run, envelope=envelope)

        n = Notification.objects.get(recipient=member_user, event_type="schedule.finished")
        assert n.context["repo_id"] == "acme/app"
        assert n.context["status"] == RunStatus.SUCCESSFUL
        assert n.context["input_tokens"] == 100
        assert n.context["output_tokens"] == 200
        assert n.context["total_tokens"] == 300
        assert n.context["cost_usd"] == 0.05

    def test_idempotent_second_emit_is_deduped(self, member_user, caplog):
        session = _session(user=member_user)
        run, envelope = _classified_run(session, status=EnvelopeStatus.FAILED, user=member_user)
        run_classified.send(sender=Run, run=run, envelope=envelope)
        with caplog.at_level(logging.DEBUG, logger="daiv.notifications"):
            run_classified.send(sender=Run, run=run, envelope=envelope)
        assert Notification.objects.filter(recipient=member_user, event_type="job.finished").count() == 1

    def test_integrity_error_without_existing_row_is_logged_as_unexpected(self, member_user, email_binding, caplog):
        """An IntegrityError that is NOT the benign dedup race (no row exists after it) is a real bug
        and must surface at ERROR, not be swallowed as 'already delivered'."""
        from django.db import IntegrityError

        session = _session(user=member_user)
        run, envelope = _classified_run(session, status=EnvelopeStatus.FAILED, user=member_user)
        with (
            patch("notifications.run_notifiers.notify", side_effect=IntegrityError("boom")),
            patch("notifications.run_notifiers._notification_exists", return_value=False),
            caplog.at_level(logging.ERROR, logger="daiv.notifications"),
        ):
            run_classified.send(sender=Run, run=run, envelope=envelope)
        assert any("Unexpected IntegrityError" in rec.message for rec in caplog.records)


@pytest.mark.django_db
class TestClassifierReasonInContext:
    """The classifier's summary and findings are what tell a recipient why they were paged, so they
    must reach the stored context every channel renders from."""

    def _emit(self, member_user, *, status=EnvelopeStatus.FOUND_ISSUES, summary="s", actionable=()):
        session = _session(user=member_user)
        run = _run(session, user=member_user, finished_at=timezone.now())
        envelope = RunEnvelope.objects.create(run=run, status=status, summary=summary, actionable=list(actionable))
        run_classified.send(sender=Run, run=run, envelope=envelope)
        return Notification.objects.get(recipient=member_user, event_type="job.finished")

    @staticmethod
    def _items(n, *, fix_prompt=None):
        items = []
        for i in range(n):
            item = {"id": str(i), "kind": "bug", "label": f"label {i}", "ref": f"app/f{i}.py", "schema_version": 1}
            if fix_prompt:
                item["fix_prompt"] = fix_prompt
            items.append(item)
        return items

    def test_summary_is_its_own_key_not_just_the_body(self, member_user, email_binding):
        # body falls back to the subject when the summary is blank, so a channel cannot recover
        # "there was no summary" from body alone.
        notification = self._emit(member_user, summary="Two flaky tests in the auth suite")
        assert notification.context["summary"] == "Two flaky tests in the auth suite"
        assert notification.body == "Two flaky tests in the auth suite"

    def test_blank_summary_leaves_the_key_empty_while_body_falls_back(self, member_user, email_binding):
        notification = self._emit(member_user, status=EnvelopeStatus.NEEDS_ATTENTION, summary="")
        assert notification.context["summary"] == ""
        assert notification.body == notification.subject

    def test_findings_travel_with_the_notification(self, member_user, email_binding):
        notification = self._emit(member_user, actionable=self._items(2))
        assert notification.context["actionable"] == [
            {"kind": "bug", "label": "label 0", "ref": "app/f0.py"},
            {"kind": "bug", "label": "label 1", "ref": "app/f1.py"},
        ]
        assert notification.context["actionable_overflow"] == 0

    def test_long_finding_lists_are_trimmed_with_an_overflow_count(self, member_user, email_binding):
        notification = self._emit(member_user, actionable=self._items(ACTIONABLE_CONTEXT_LIMIT + 3))
        assert len(notification.context["actionable"]) == ACTIONABLE_CONTEXT_LIMIT
        assert notification.context["actionable_overflow"] == 3

    def test_a_null_slot_travels_empty_rather_than_as_the_word_none(self, member_user, email_binding):
        item = {"id": "0", "kind": None, "label": "orphaned finding", "ref": None, "schema_version": 1}
        notification = self._emit(member_user, actionable=[item])
        assert notification.context["actionable"] == [{"kind": "", "label": "orphaned finding", "ref": ""}]

    def test_an_exactly_full_finding_list_reports_no_overflow(self, member_user, email_binding):
        notification = self._emit(member_user, actionable=self._items(ACTIONABLE_CONTEXT_LIMIT))
        assert len(notification.context["actionable"]) == ACTIONABLE_CONTEXT_LIMIT
        assert notification.context["actionable_overflow"] == 0

    def test_fix_prompt_never_leaves_the_envelope(self, member_user, email_binding):
        # fix_prompt seeds a Finding -> Fix agent; it is not recipient-facing copy.
        notification = self._emit(member_user, actionable=self._items(1, fix_prompt="rewrite the fixture"))
        assert notification.context["actionable"][0].keys() == {"kind", "label", "ref"}

    @pytest.mark.parametrize("status", [EnvelopeStatus.NEEDS_ATTENTION, EnvelopeStatus.FAILED])
    def test_finding_free_statuses_carry_an_empty_list(self, member_user, email_binding, status):
        notification = self._emit(member_user, status=status)
        assert notification.context["actionable"] == []
        assert notification.context["actionable_overflow"] == 0


@pytest.mark.django_db
class TestBatchNotableRuns:
    """A rollup says how many runs need a look; these rows say which ones and why. One row per run,
    not per finding — nesting a 20-repo batch's findings would cap away most of them."""

    def _rollup(self, member_user, pairs, *, event_type="job_batch.finished"):
        """Classify one sibling per (status, summary) pair and return the emitted context."""
        runs = _make_run_batch(member_user, statuses=[RunStatus.SUCCESSFUL] * len(pairs))
        for run, (status, summary) in zip(runs, pairs, strict=True):
            run.finished_at = timezone.now()
            run.save(update_fields=["finished_at"])
            envelope = _classify(run, status, summary=summary)
            run_classified.send(sender=Run, run=run, envelope=envelope)
        return Notification.objects.get(recipient=member_user, event_type=event_type).context

    def test_each_notable_sibling_contributes_its_repo_and_summary(self, member_user, email_binding):
        ctx = self._rollup(
            member_user,
            [(EnvelopeStatus.FAILED, "migration 0042 errored"), (EnvelopeStatus.ALL_CLEAR, "nothing to report")],
        )
        assert ctx["notable_runs"] == [{"kind": "Failed", "label": "acme/repo0", "ref": "migration 0042 errored"}]
        assert ctx["notable_runs_overflow"] == 0

    def test_all_clear_siblings_are_left_out(self, member_user, email_binding):
        ctx = self._rollup(
            member_user, [(EnvelopeStatus.NEEDS_ATTENTION, "drifted"), (EnvelopeStatus.ALL_CLEAR, "clean")]
        )
        assert [row["kind"] for row in ctx["notable_runs"]] == ["Needs attention"]

    @pytest.mark.parametrize("status", sorted(notify_worthy_statuses()))
    def test_every_notify_worthy_status_counts_toward_the_rollup(self, member_user, email_binding, status):
        """A status the batch aggregate misses leaves ``notable`` at zero and the whole batch is
        dismissed as clean — no rollup, no error."""
        ctx = self._rollup(member_user, [(status, "s"), (EnvelopeStatus.ALL_CLEAR, "clean")])
        assert ctx["notable_count"] == 1

    def test_rows_are_ordered_worst_first(self, member_user, email_binding):
        ctx = self._rollup(
            member_user,
            [
                (EnvelopeStatus.NEEDS_ATTENTION, "n"),
                (EnvelopeStatus.FAILED, "f"),
                (EnvelopeStatus.ALL_CLEAR, "c"),
                (EnvelopeStatus.FOUND_ISSUES, "i"),
            ],
        )
        assert [row["kind"] for row in ctx["notable_runs"]] == ["Failed", "Found issues", "Needs attention"]

    def test_the_cap_keeps_the_worst_rows_and_counts_the_rest(self, member_user, email_binding):
        # The failure is created *first*, so the batch's own newest-first ordering buries it below
        # every needs-attention sibling: only the severity sort can lift it back inside the cap.
        pairs = [(EnvelopeStatus.FAILED, "the one that matters")]
        pairs += [(EnvelopeStatus.NEEDS_ATTENTION, f"n{i}") for i in range(NOTABLE_RUNS_CONTEXT_LIMIT + 2)]
        ctx = self._rollup(member_user, pairs)

        assert len(ctx["notable_runs"]) == NOTABLE_RUNS_CONTEXT_LIMIT
        assert ctx["notable_runs_overflow"] == 3
        assert [row["kind"] for row in ctx["notable_runs"]] == ["Failed"] + ["Needs attention"] * 4
        assert ctx["notable_runs"][0] == {"kind": "Failed", "label": "acme/repo0", "ref": "the one that matters"}

    def test_a_sibling_with_no_summary_still_names_its_repo(self, member_user, email_binding):
        ctx = self._rollup(member_user, [(EnvelopeStatus.FAILED, ""), (EnvelopeStatus.ALL_CLEAR, "clean")])
        assert ctx["notable_runs"] == [{"kind": "Failed", "label": "acme/repo0", "ref": ""}]

    def test_a_single_run_batch_carries_findings_not_notable_rows(self, member_user, email_binding):
        """total == 1 falls through to the per-run path, which has the full findings list instead."""
        ctx = self._rollup(member_user, [(EnvelopeStatus.FOUND_ISSUES, "one repo")], event_type="job.finished")
        assert "notable_runs" not in ctx
        assert ctx["actionable"] == [{"kind": "bug", "label": "x", "ref": "y"}]


@pytest.mark.django_db
class TestRunFanoutToSubscribers:
    """Single-run schedule notifications fan out to owner + subscribers."""

    def test_owner_plus_two_subscribers_each_get_one_notification(self, member_user, run_schedule):
        sub1 = User.objects.create_user(username="sub1", email="sub1@test.com", password="x")  # noqa: S106
        sub2 = User.objects.create_user(username="sub2", email="sub2@test.com", password="x")  # noqa: S106
        run_schedule.subscribers.add(sub1, sub2)

        session = _session(origin=SessionOrigin.SCHEDULE, thread_id=str(uuid.uuid4()), scheduled_job=run_schedule)
        run, envelope = _classified_run(
            session, status=EnvelopeStatus.FOUND_ISSUES, trigger_type=SessionOrigin.SCHEDULE, user=member_user
        )
        run_classified.send(sender=Run, run=run, envelope=envelope)

        assert Notification.objects.filter(recipient=member_user).count() == 1
        assert Notification.objects.filter(recipient=sub1).count() == 1
        assert Notification.objects.filter(recipient=sub2).count() == 1

    def test_owner_accidentally_in_subscribers_still_one_notification(self, member_user, run_schedule):
        run_schedule.subscribers.add(member_user)
        session = _session(origin=SessionOrigin.SCHEDULE, thread_id=str(uuid.uuid4()), scheduled_job=run_schedule)
        run, envelope = _classified_run(
            session, status=EnvelopeStatus.FOUND_ISSUES, trigger_type=SessionOrigin.SCHEDULE, user=member_user
        )
        run_classified.send(sender=Run, run=run, envelope=envelope)
        assert Notification.objects.filter(recipient=member_user).count() == 1

    def test_one_recipient_failure_does_not_block_others(self, member_user, run_schedule, mocker):
        from notifications.services import notify as real_notify

        sub1 = User.objects.create_user(username="fsub1", email="fsub1@test.com", password="x")  # noqa: S106
        sub2 = User.objects.create_user(username="fsub2", email="fsub2@test.com", password="x")  # noqa: S106
        run_schedule.subscribers.add(sub1, sub2)

        def flaky_notify(*, recipient, **kwargs):
            if recipient.pk == sub1.pk:
                raise RuntimeError("boom")
            return real_notify(recipient=recipient, **kwargs)

        mocker.patch("notifications.run_notifiers.notify", side_effect=flaky_notify)

        session = _session(origin=SessionOrigin.SCHEDULE, thread_id=str(uuid.uuid4()), scheduled_job=run_schedule)
        run, envelope = _classified_run(
            session, status=EnvelopeStatus.FOUND_ISSUES, trigger_type=SessionOrigin.SCHEDULE, user=member_user
        )
        run_classified.send(sender=Run, run=run, envelope=envelope)

        assert Notification.objects.filter(recipient=member_user).count() == 1
        assert Notification.objects.filter(recipient=sub1).count() == 0
        assert Notification.objects.filter(recipient=sub2).count() == 1
