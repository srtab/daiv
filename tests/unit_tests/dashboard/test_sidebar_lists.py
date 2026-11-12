import uuid
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

import pytest
from dashboard.sidebar import (
    FIRED_WITHIN,
    FIRINGS_PER_SCHEDULE,
    SCHEDULES_LIMIT,
    SESSIONS_PAGE,
    SidebarLists,
    schedule_rows,
    session_rows,
    sidebar_lists,
)
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


def _firing(schedule, *statuses, minutes_ago=0):
    """One dispatch of ``schedule``: a session per status, each with a schedule run of one shared batch."""
    batch_id = uuid.uuid4()
    sessions = []
    for status in statuses:
        session = _session(
            schedule.user, minutes_ago=minutes_ago, scheduled_job=schedule, origin=SessionOrigin.SCHEDULE
        )
        _run(session, status, minutes_ago=minutes_ago, batch_id=batch_id)
        sessions.append(session)
    return batch_id, sessions


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


@pytest.mark.django_db
class TestScheduleRows:
    def test_lists_owned_and_subscribed_enabled_schedules(self, member_user, admin_user):
        _schedule(member_user, name="Own")
        _schedule(admin_user, name="Subscribed").subscribers.add(member_user)
        _schedule(admin_user, name="Theirs")

        assert {row.name for row in schedule_rows(member_user)} == {"Own", "Subscribed"}

    def test_schedule_with_several_subscribers_is_listed_once(self, member_user, admin_user):
        shared = _schedule(member_user)
        shared.subscribers.add(admin_user, member_user)

        assert [row.pk for row in schedule_rows(member_user)] == [shared.pk]

    def test_admin_lists_only_their_own(self, admin_user, member_user):
        _schedule(member_user, name="Member's")
        _schedule(admin_user, name="Admin's")

        assert [row.name for row in schedule_rows(admin_user)] == ["Admin's"]

    @pytest.mark.parametrize(
        ("fired_days_ago", "listed"), [(FIRED_WITHIN.days - 1, True), (FIRED_WITHIN.days + 1, False), (None, False)]
    )
    def test_disabled_schedule_shows_for_14_days_after_firing(self, member_user, fired_days_ago, listed):
        last_run_at = None if fired_days_ago is None else timezone.now() - timedelta(days=fired_days_ago)
        _schedule(member_user, is_enabled=False, last_run_at=last_run_at)

        assert bool(schedule_rows(member_user)) is listed

    def test_orders_next_run_first_then_most_recently_fired(self, member_user):
        now = timezone.now()
        _schedule(member_user, name="A", next_run_at=now + timedelta(hours=6))
        _schedule(member_user, name="B", next_run_at=now + timedelta(hours=1))
        _schedule(member_user, name="C", is_enabled=False, last_run_at=now - timedelta(days=1))
        _schedule(member_user, name="D", is_enabled=False, last_run_at=now - timedelta(days=2))

        assert [row.name for row in schedule_rows(member_user)] == ["B", "A", "C", "D"]

    def test_caps_at_ten(self, member_user):
        for index in range(SCHEDULES_LIMIT + 1):
            _schedule(member_user, name=f"S{index}")

        assert len(schedule_rows(member_user)) == SCHEDULES_LIMIT

    def test_all_runs_filters_the_sessions_page_by_schedule(self, member_user):
        schedule = _schedule(member_user)

        (row,) = schedule_rows(member_user)

        assert row.all_runs_url == reverse("session_list") + f"?schedule={schedule.pk}"

    def test_one_firing_per_batch_with_repo_count(self, member_user):
        schedule = _schedule(member_user)
        batch_id, _ = _firing(schedule, RunStatus.SUCCESSFUL, RunStatus.SUCCESSFUL, RunStatus.SUCCESSFUL)

        ((firing,),) = [row.firings for row in schedule_rows(member_user)]

        assert firing.repo_count == 3
        assert firing.url == reverse("session_list") + f"?batch={batch_id}"

    def test_single_session_firing_links_the_session(self, member_user):
        _, (session,) = _firing(_schedule(member_user), RunStatus.SUCCESSFUL)

        ((firing,),) = [row.firings for row in schedule_rows(member_user)]

        assert firing.repo_count == 1
        assert firing.url == reverse("session_detail", kwargs={"thread_id": session.thread_id})

    def test_firing_status_is_the_worst(self, member_user):
        schedule = _schedule(member_user)
        _firing(schedule, RunStatus.SUCCESSFUL, RunStatus.RUNNING, RunStatus.FAILED, minutes_ago=60)
        _firing(schedule, RunStatus.SUCCESSFUL, RunStatus.RUNNING, minutes_ago=10)

        (row,) = schedule_rows(member_user)

        assert [firing.status for firing in row.firings] == [RunStatus.RUNNING, RunStatus.FAILED]

    def test_later_chat_run_does_not_make_a_second_firing(self, member_user):
        _, (session,) = _firing(_schedule(member_user), RunStatus.SUCCESSFUL, minutes_ago=30)
        _run(session, RunStatus.SUCCESSFUL, minutes_ago=20, trigger_type=SessionOrigin.API_JOB, batch_id=uuid.uuid4())
        _run(session, RunStatus.RUNNING, minutes_ago=10, trigger_type=SessionOrigin.CHAT)

        ((firing,),) = [row.firings for row in schedule_rows(member_user)]

        assert firing.status == RunStatus.RUNNING

    def test_keeps_three_newest_firings(self, member_user):
        schedule = _schedule(member_user)
        sessions = [_firing(schedule, RunStatus.SUCCESSFUL, minutes_ago=m)[1][0] for m in (50, 40, 30, 20, 10)]

        (row,) = schedule_rows(member_user)

        newest = reversed(sessions[-FIRINGS_PER_SCHEDULE:])
        assert [firing.url for firing in row.firings] == [
            reverse("session_detail", kwargs={"thread_id": session.thread_id}) for session in newest
        ]

    def test_archived_sessions_leave_a_schedule_without_firings(self, member_user):
        _, (session,) = _firing(_schedule(member_user), RunStatus.FAILED)
        HiddenSession.objects.create(session=session, user=member_user)

        (row,) = schedule_rows(member_user)

        assert row.firings == ()
        assert row.status == ""
        assert row.starts_open is False

    @pytest.mark.parametrize(
        ("latest", "starts_open"),
        [
            (RunStatus.FAILED, True),
            (RunStatus.WAITING_INPUT, True),
            (RunStatus.RUNNING, False),
            (RunStatus.SUCCESSFUL, False),
        ],
    )
    def test_starts_open_on_failed_or_waiting_latest_firing(self, member_user, latest, starts_open):
        schedule = _schedule(member_user)
        _firing(schedule, RunStatus.FAILED, minutes_ago=60)
        _firing(schedule, latest, minutes_ago=10)

        (row,) = schedule_rows(member_user)

        assert row.starts_open is starts_open

    def test_viewed_scheduled_session_opens_its_schedule(self, member_user):
        schedule = _schedule(member_user)
        _, (viewed,) = _firing(schedule, RunStatus.SUCCESSFUL, minutes_ago=50)
        for minutes_ago in (40, 30, 20, 10):
            _firing(schedule, RunStatus.SUCCESSFUL, minutes_ago=minutes_ago)

        (row,) = schedule_rows(member_user, viewing=viewed.thread_id)

        assert row.starts_open is True
        assert [firing.active for firing in row.firings] == [False, False, False, True]

    def test_viewed_session_among_the_newest_is_not_added_twice(self, member_user):
        schedule = _schedule(member_user)
        for minutes_ago in (40, 30, 20):
            _firing(schedule, RunStatus.SUCCESSFUL, minutes_ago=minutes_ago)
        _, (viewed,) = _firing(schedule, RunStatus.SUCCESSFUL, minutes_ago=10)

        (row,) = schedule_rows(member_user, viewing=viewed.thread_id)

        assert [firing.active for firing in row.firings] == [True, False, False]

    @pytest.mark.parametrize(("latest", "status"), [(RunStatus.SUCCESSFUL, ""), (RunStatus.RUNNING, RunStatus.RUNNING)])
    def test_status_only_for_state_word_statuses(self, member_user, latest, status):
        _firing(_schedule(member_user), latest)

        (row,) = schedule_rows(member_user)

        assert row.status == status

    def test_query_count_is_fixed(self, member_user, django_assert_max_num_queries):
        _firing(_schedule(member_user), RunStatus.SUCCESSFUL)
        with django_assert_max_num_queries(3):
            schedule_rows(member_user)

        for index in range(SCHEDULES_LIMIT - 1):
            schedule = _schedule(member_user, name=f"S{index}")
            for minutes_ago in range(FIRINGS_PER_SCHEDULE + 1):
                _, sessions = _firing(schedule, RunStatus.FAILED, RunStatus.SUCCESSFUL, minutes_ago=minutes_ago)
        with django_assert_max_num_queries(3):
            schedule_rows(member_user, viewing=sessions[0].thread_id)

    def test_sidebar_lists_combines_both(self, member_user):
        own = _session(member_user)
        schedule = _schedule(member_user)

        lists = sidebar_lists(member_user)

        assert isinstance(lists, SidebarLists)
        assert [row.thread_id for row in lists.sessions] == [own.thread_id]
        assert lists.has_more is False
        assert [row.pk for row in lists.schedules] == [schedule.pk]
