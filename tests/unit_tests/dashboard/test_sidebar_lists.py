import uuid
from datetime import timedelta

from django.utils import timezone

import pytest
from dashboard.sidebar import SESSIONS_PAGE, session_rows
from sessions.models import HiddenSession, Run, RunStatus, Session, SessionOrigin

from schedules.models import Frequency, ScheduledJob


def _schedule(user, **fields) -> ScheduledJob:
    defaults = {
        "name": "Nightly audit",
        "prompt": "p",
        "repos": [{"repo_id": "daiv/test", "ref": ""}],
        "frequency": Frequency.DAILY,
        "time": "09:00",
    }
    return ScheduledJob.objects.create(user=user, **(defaults | fields))


def _session(user, *, minutes_ago=0, title="A session", scheduled_job=None, origin=SessionOrigin.CHAT) -> Session:
    return Session.objects.create(
        thread_id=str(uuid.uuid4()),
        origin=origin,
        repo_id="daiv/test",
        user=user,
        title=title,
        scheduled_job=scheduled_job,
        last_active_at=timezone.now() - timedelta(minutes=minutes_ago),
    )


def _run(session, status, *, minutes_ago=0, trigger_type=None, batch_id=None) -> Run:
    run = Run.objects.create(
        session=session,
        status=status,
        trigger_type=trigger_type or session.origin,
        repo_id=session.repo_id,
        user=session.user,
        batch_id=batch_id,
    )
    Run.objects.filter(pk=run.pk).update(created_at=timezone.now() - timedelta(minutes=minutes_ago))
    return run


@pytest.mark.django_db
class TestSessionRows:
    def test_lists_own_unscheduled_sessions_newest_first(self, member_user):
        oldest = _session(member_user, minutes_ago=30)
        newest = _session(member_user, minutes_ago=10)
        middle = _session(member_user, minutes_ago=20)

        rows, _ = session_rows(member_user)

        assert [row.thread_id for row in rows] == [newest.thread_id, middle.thread_id, oldest.thread_id]

    def test_leaves_out_scheduled_archived_and_other_peoples(self, member_user, admin_user):
        _session(member_user, scheduled_job=_schedule(member_user), origin=SessionOrigin.SCHEDULE)
        archived = _session(member_user)
        HiddenSession.objects.create(session=archived, user=member_user)
        _session(admin_user)

        rows, has_more = session_rows(member_user)

        assert rows == ()
        assert has_more is False

    def test_admin_lists_only_their_own(self, admin_user, member_user):
        _session(member_user)
        own = _session(admin_user)

        rows, _ = session_rows(admin_user)

        assert [row.thread_id for row in rows] == [own.thread_id]

    def test_caps_at_limit_and_reports_more(self, member_user):
        for minutes_ago in range(SESSIONS_PAGE):
            _session(member_user, minutes_ago=minutes_ago)

        rows, has_more = session_rows(member_user)
        assert (len(rows), has_more) == (8, False)

        _session(member_user, minutes_ago=99)
        rows, has_more = session_rows(member_user)
        assert (len(rows), has_more) == (8, True)

    def test_status_is_the_latest_runs(self, member_user):
        session = _session(member_user)
        _run(session, RunStatus.SUCCESSFUL, minutes_ago=5)
        _run(session, RunStatus.FAILED, minutes_ago=1)

        (row,), _ = session_rows(member_user)

        assert row.status == RunStatus.FAILED
        assert row.shows_state is True

    def test_successful_run_shows_no_state(self, member_user):
        _run(_session(member_user), RunStatus.SUCCESSFUL)

        (row,), _ = session_rows(member_user)

        assert row.shows_state is False

    def test_session_without_runs_has_blank_status(self, member_user):
        _session(member_user, title="")

        (row,), _ = session_rows(member_user)

        assert row.status == ""
        assert row.title == ""
        assert row.shows_state is False

    def test_viewed_session_is_marked_active(self, member_user):
        viewed = _session(member_user, minutes_ago=5)
        _session(member_user)

        rows, _ = session_rows(member_user, viewing=viewed.thread_id)

        assert [row.active for row in rows] == [False, True]

    def test_viewed_session_outside_the_page_is_appended(self, member_user):
        oldest = _session(member_user, minutes_ago=99)
        for minutes_ago in range(SESSIONS_PAGE):
            _session(member_user, minutes_ago=minutes_ago)

        rows, has_more = session_rows(member_user, viewing=oldest.thread_id)

        assert len(rows) == 9
        assert rows[-1].thread_id == oldest.thread_id
        assert rows[-1].active is True
        assert has_more is True

    def test_viewing_someone_elses_session_adds_nothing(self, member_user, admin_user):
        for minutes_ago in range(SESSIONS_PAGE):
            _session(member_user, minutes_ago=minutes_ago)
        theirs = _session(admin_user)

        rows, _ = session_rows(member_user, viewing=theirs.thread_id)

        assert len(rows) == 8
        assert not any(row.active for row in rows)

    @pytest.mark.parametrize("count", [3, 9])
    def test_query_count_is_fixed(self, member_user, django_assert_max_num_queries, count):
        sessions = [_session(member_user, minutes_ago=minutes_ago) for minutes_ago in range(count)]
        for session in sessions:
            _run(session, RunStatus.RUNNING)

        with django_assert_max_num_queries(1):
            session_rows(member_user)
        with django_assert_max_num_queries(2):
            session_rows(member_user, viewing=sessions[-1].thread_id)
