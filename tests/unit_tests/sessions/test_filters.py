from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta

from django.utils import timezone

import pytest
from sessions.filters import SessionFilter
from sessions.models import Run, RunStatus, Session, SessionOrigin

from accounts.models import User
from schedules.models import Frequency, ScheduledJob


@pytest.fixture
def user(db):
    return User.objects.create_user(
        username="alice",
        email="alice@test.com",
        password="testpass123",  # noqa: S106
    )


def _create_session(**kwargs) -> Session:
    defaults = {
        "thread_id": str(uuid.uuid4()),
        "origin": SessionOrigin.SCHEDULE,
        "repo_id": "group/project",
        "ref": "main",
    }
    defaults.update(kwargs)
    return Session.objects.create(**defaults)


def _create_run(session: Session, **kwargs) -> Run:
    defaults = {
        "session": session,
        "trigger_type": SessionOrigin.SCHEDULE,
        "repo_id": session.repo_id,
        "status": RunStatus.SUCCESSFUL,
    }
    defaults.update(kwargs)
    return Run.objects.create(**defaults)


def _qs():
    """Base queryset with the annotation required by filter_status."""
    return Session.objects.with_latest_status()


@pytest.mark.django_db
class TestSessionFilter:
    def test_no_params_returns_all(self, user):
        a = _create_session()
        b = _create_session()
        qs = SessionFilter({}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert a.pk in pks
        assert b.pk in pks

    def test_trigger_filter(self, user):
        sched = _create_session(origin=SessionOrigin.SCHEDULE)
        webhook = _create_session(origin=SessionOrigin.ISSUE_WEBHOOK)
        qs = SessionFilter({"trigger": SessionOrigin.SCHEDULE}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert sched.pk in pks
        assert webhook.pk not in pks

    def test_repo_filter(self, user):
        a = _create_session(repo_id="group/project")
        b = _create_session(repo_id="group/other")
        qs = SessionFilter({"repo": "group/project"}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert a.pk in pks
        assert b.pk not in pks

    def test_schedule_filter_invalid_is_ignored(self, user):
        a = _create_session()
        f = SessionFilter({"schedule": "not-a-number"}, queryset=_qs())
        assert not f.form.is_valid()
        assert a.pk in list(f.qs.values_list("pk", flat=True))

    def test_schedule_filter_matches_fk(self, user):
        job = ScheduledJob.objects.create(
            user=user,
            name="nightly",
            prompt="x",
            repos=[{"repo_id": "group/project", "ref": ""}],
            frequency=Frequency.DAILY,
            time=time(3, 0),
        )
        match = _create_session(scheduled_job=job)
        other = _create_session()
        qs = SessionFilter({"schedule": str(job.pk)}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert match.pk in pks
        assert other.pk not in pks

    def test_date_from_filter(self, user):
        old = _create_session()
        Session.objects.filter(pk=old.pk).update(last_active_at=datetime(2020, 1, 1, tzinfo=UTC))
        recent = _create_session()
        Session.objects.filter(pk=recent.pk).update(last_active_at=datetime(2026, 1, 1, tzinfo=UTC))
        qs = SessionFilter({"date_from": "2025-06-01"}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert recent.pk in pks
        assert old.pk not in pks

    def test_date_to_filter(self, user):
        old = _create_session()
        Session.objects.filter(pk=old.pk).update(last_active_at=datetime(2020, 1, 1, tzinfo=UTC))
        recent = _create_session()
        Session.objects.filter(pk=recent.pk).update(last_active_at=datetime(2026, 1, 1, tzinfo=UTC))
        qs = SessionFilter({"date_to": "2025-06-01"}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert old.pk in pks
        assert recent.pk not in pks

    def test_date_range_combined(self, user):
        before = _create_session()
        Session.objects.filter(pk=before.pk).update(last_active_at=datetime(2020, 1, 1, tzinfo=UTC))
        inside = _create_session()
        Session.objects.filter(pk=inside.pk).update(last_active_at=datetime(2025, 6, 15, tzinfo=UTC))
        after = _create_session()
        Session.objects.filter(pk=after.pk).update(last_active_at=datetime(2026, 1, 1, tzinfo=UTC))
        qs = SessionFilter({"date_from": "2025-01-01", "date_to": "2025-12-31"}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert inside.pk in pks
        assert before.pk not in pks
        assert after.pk not in pks

    def test_invalid_date_is_ignored(self, user):
        a = _create_session()
        f = SessionFilter({"date_from": "not-a-date"}, queryset=_qs())
        assert not f.form.is_valid()
        assert a.pk in list(f.qs.values_list("pk", flat=True))

    def test_combined_filters(self, user):
        match = _create_session(origin=SessionOrigin.SCHEDULE, repo_id="group/project")
        _create_run(match, status=RunStatus.SUCCESSFUL)
        wrong_origin = _create_session(origin=SessionOrigin.ISSUE_WEBHOOK, repo_id="group/project")
        _create_run(wrong_origin, status=RunStatus.SUCCESSFUL)
        wrong_repo = _create_session(origin=SessionOrigin.SCHEDULE, repo_id="group/other")
        _create_run(wrong_repo, status=RunStatus.SUCCESSFUL)

        qs = SessionFilter({"trigger": SessionOrigin.SCHEDULE, "repo": "group/project"}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert match.pk in pks
        assert wrong_origin.pk not in pks
        assert wrong_repo.pk not in pks

    # --- Brief-specified new tests ---

    def test_status_filters_on_latest_run(self, user):
        """?status=RUNNING matches a session whose LATEST run is RUNNING, and does not
        match a session whose latest run is SUCCESSFUL even if an older one was RUNNING."""
        # Session A: older RUNNING, newer SUCCESSFUL → should NOT match ?status=RUNNING
        session_a = _create_session()
        _create_run(session_a, status=RunStatus.RUNNING)
        _create_run(session_a, status=RunStatus.SUCCESSFUL)

        # Session B: only run is RUNNING → SHOULD match
        session_b = _create_session()
        _create_run(session_b, status=RunStatus.RUNNING)

        qs = SessionFilter({"status": RunStatus.RUNNING}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert session_b.pk in pks
        assert session_a.pk not in pks

    def test_trigger_filters_on_origin(self, user):
        """?trigger=issue_webhook returns webhook-origin sessions; ?trigger=chat returns chat sessions."""
        webhook_session = _create_session(origin=SessionOrigin.ISSUE_WEBHOOK)
        chat_session = _create_session(origin=SessionOrigin.CHAT)
        other_session = _create_session(origin=SessionOrigin.SCHEDULE)

        webhook_qs = SessionFilter({"trigger": SessionOrigin.ISSUE_WEBHOOK}, queryset=_qs()).qs
        webhook_pks = list(webhook_qs.values_list("pk", flat=True))
        assert webhook_session.pk in webhook_pks
        assert chat_session.pk not in webhook_pks
        assert other_session.pk not in webhook_pks

        chat_qs = SessionFilter({"trigger": SessionOrigin.CHAT}, queryset=_qs()).qs
        chat_pks = list(chat_qs.values_list("pk", flat=True))
        assert chat_session.pk in chat_pks
        assert webhook_session.pk not in chat_pks

    def test_batch_filters_via_runs(self, user):
        """?batch=<uuid> returns sessions containing a run with that batch_id."""
        batch_id = uuid.uuid4()

        session_with_batch = _create_session()
        _create_run(session_with_batch, batch_id=batch_id)

        session_other_batch = _create_session()
        _create_run(session_other_batch, batch_id=uuid.uuid4())

        session_no_batch = _create_session()

        qs = SessionFilter({"batch": str(batch_id)}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert session_with_batch.pk in pks
        assert session_other_batch.pk not in pks
        assert session_no_batch.pk not in pks

    def test_invalid_status_drops_filter(self, user):
        """?status=bogus returns the unfiltered list (strict=False semantics)."""
        a = _create_session()
        b = _create_session()
        f = SessionFilter({"status": "bogus"}, queryset=_qs())
        assert not f.form.is_valid()
        # Invalid choice is dropped → no filter applied; all sessions returned.
        pks = list(f.qs.values_list("pk", flat=True))
        assert a.pk in pks
        assert b.pk in pks

    def test_batch_filter_invalid_uuid_is_ignored(self, user):
        a = _create_session()
        f = SessionFilter({"batch": "not-a-uuid"}, queryset=_qs())
        assert not f.form.is_valid()
        assert a.pk in list(f.qs.values_list("pk", flat=True))

    def test_q_matches_title(self, user):
        match = _create_session(title="Fix the crash", repo_id="group/project")
        other = _create_session(title="Add a feature", repo_id="group/project")
        qs = SessionFilter({"q": "crash"}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert match.pk in pks
        assert other.pk not in pks

    def test_q_matches_repo_case_insensitive(self, user):
        match = _create_session(title="", repo_id="Group/Payments")
        other = _create_session(title="", repo_id="group/other")
        qs = SessionFilter({"q": "payments"}, queryset=_qs()).qs
        pks = list(qs.values_list("pk", flat=True))
        assert match.pk in pks
        assert other.pk not in pks

    def test_q_empty_is_noop(self, user):
        a = _create_session()
        b = _create_session()
        pks = list(SessionFilter({"q": ""}, queryset=_qs()).qs.values_list("pk", flat=True))
        assert a.pk in pks
        assert b.pk in pks

    def test_range_7d_includes_recent_excludes_old(self, user):
        now = timezone.now()
        recent = _create_session()
        Session.objects.filter(pk=recent.pk).update(last_active_at=now - timedelta(days=2))
        old = _create_session()
        Session.objects.filter(pk=old.pk).update(last_active_at=now - timedelta(days=20))
        pks = list(SessionFilter({"range": "7d"}, queryset=_qs()).qs.values_list("pk", flat=True))
        assert recent.pk in pks
        assert old.pk not in pks

    def test_range_today_uses_local_midnight(self, user):
        now = timezone.now()
        today = _create_session()
        Session.objects.filter(pk=today.pk).update(last_active_at=now)
        yesterday = _create_session()
        Session.objects.filter(pk=yesterday.pk).update(last_active_at=now - timedelta(days=1, hours=1))
        pks = list(SessionFilter({"range": "today"}, queryset=_qs()).qs.values_list("pk", flat=True))
        assert today.pk in pks
        assert yesterday.pk not in pks

    def test_range_invalid_is_ignored(self, user):
        a = _create_session()
        f = SessionFilter({"range": "bogus"}, queryset=_qs())
        assert not f.form.is_valid()
        assert a.pk in list(f.qs.values_list("pk", flat=True))

    def test_q_matches_both_title_and_repo_returns_row_once(self, user):
        # filter_q ORs two columns of the SAME row (no join), so a term hitting both
        # title and repo_id must not duplicate the row.
        match = _create_session(title="payments dashboard", repo_id="group/payments")
        pks = list(SessionFilter({"q": "payments"}, queryset=_qs()).qs.values_list("pk", flat=True))
        assert pks.count(match.pk) == 1

    def test_range_2d_and_30d_windows(self, user):
        now = timezone.now()
        d1 = _create_session()
        Session.objects.filter(pk=d1.pk).update(last_active_at=now - timedelta(days=1))
        d3 = _create_session()
        Session.objects.filter(pk=d3.pk).update(last_active_at=now - timedelta(days=3))
        d20 = _create_session()
        Session.objects.filter(pk=d20.pk).update(last_active_at=now - timedelta(days=20))
        d40 = _create_session()
        Session.objects.filter(pk=d40.pk).update(last_active_at=now - timedelta(days=40))

        pks_2d = set(SessionFilter({"range": "2d"}, queryset=_qs()).qs.values_list("pk", flat=True))
        assert d1.pk in pks_2d
        assert d3.pk not in pks_2d
        assert d20.pk not in pks_2d

        pks_30d = set(SessionFilter({"range": "30d"}, queryset=_qs()).qs.values_list("pk", flat=True))
        assert {d1.pk, d3.pk, d20.pk} <= pks_30d
        assert d40.pk not in pks_30d

    def test_range_boundary_is_inclusive(self, user):
        # last_active_at__gte makes the window edge inclusive. Use ~1h margins around the
        # 7-day edge so the assertion can't flake on the sub-second gap between the test's
        # ``now`` and filter_range's own ``timezone.now()``.
        now = timezone.now()
        just_in = _create_session()
        Session.objects.filter(pk=just_in.pk).update(last_active_at=now - timedelta(days=6, hours=23))
        just_out = _create_session()
        Session.objects.filter(pk=just_out.pk).update(last_active_at=now - timedelta(days=7, hours=1))
        pks = set(SessionFilter({"range": "7d"}, queryset=_qs()).qs.values_list("pk", flat=True))
        assert just_in.pk in pks
        assert just_out.pk not in pks
