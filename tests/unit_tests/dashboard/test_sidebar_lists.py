import uuid
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

import pytest
from dashboard.sidebar import (
    FIRED_WITHIN,
    FIRINGS_PER_SCHEDULE,
    SCHEDULES_LIMIT,
    SESSIONS_MAX,
    SESSIONS_PAGE,
    Sidebar,
    SidebarLists,
)
from sessions.models import RunStatus, Session, SessionOrigin
from sessions.validators import MAX_REPOS_PER_BATCH

from tests.unit_tests.dashboard.factories import make_firing, make_run, make_schedule, make_session


@pytest.mark.django_db
class TestSessionRows:
    def test_lists_own_unscheduled_sessions_newest_first(self, member_user):
        oldest = make_session(member_user, minutes_ago=30)
        newest = make_session(member_user, minutes_ago=10)
        middle = make_session(member_user, minutes_ago=20)

        rows, _ = Sidebar(member_user).session_rows()

        assert [row.thread_id for row in rows] == [newest.thread_id, middle.thread_id, oldest.thread_id]

    def test_leaves_out_scheduled_archived_and_other_peoples(self, member_user, admin_user):
        make_session(member_user, scheduled_job=make_schedule(member_user), origin=SessionOrigin.SCHEDULE)
        archived = make_session(member_user)
        Session.objects.filter(pk=archived.pk).update(archived_at=timezone.now())
        make_session(admin_user)

        rows, has_more = Sidebar(member_user).session_rows()

        assert rows == ()
        assert has_more is False

    def test_admin_lists_only_their_own(self, admin_user, member_user):
        make_session(member_user)
        own = make_session(admin_user)

        rows, _ = Sidebar(admin_user).session_rows()

        assert [row.thread_id for row in rows] == [own.thread_id]

    def test_caps_at_limit_and_reports_more(self, member_user):
        for minutes_ago in range(SESSIONS_PAGE):
            make_session(member_user, minutes_ago=minutes_ago)

        rows, has_more = Sidebar(member_user).session_rows()
        assert (len(rows), has_more) == (8, False)

        make_session(member_user, minutes_ago=99)
        rows, has_more = Sidebar(member_user).session_rows()
        assert (len(rows), has_more) == (8, True)

    def test_status_is_the_latest_runs(self, member_user):
        session = make_session(member_user)
        make_run(session, RunStatus.SUCCESSFUL, minutes_ago=5)
        make_run(session, RunStatus.FAILED, minutes_ago=1)

        (row,), _ = Sidebar(member_user).session_rows()

        assert row.status == RunStatus.FAILED
        assert row.shows_state is True

    def test_successful_run_shows_no_state(self, member_user):
        make_run(make_session(member_user), RunStatus.SUCCESSFUL)

        (row,), _ = Sidebar(member_user).session_rows()

        assert row.shows_state is False

    def test_session_without_runs_has_blank_status(self, member_user):
        make_session(member_user, title="")

        (row,), _ = Sidebar(member_user).session_rows()

        assert row.status == ""
        assert row.title == ""
        assert row.shows_state is False

    def test_viewed_session_is_marked_active(self, member_user):
        viewed = make_session(member_user, minutes_ago=5)
        make_session(member_user)

        rows, _ = Sidebar(member_user, viewing=viewed.thread_id).session_rows()

        assert [row.active for row in rows] == [False, True]

    def test_viewed_session_outside_the_page_is_appended(self, member_user):
        oldest = make_session(member_user, minutes_ago=99)
        for minutes_ago in range(SESSIONS_PAGE):
            make_session(member_user, minutes_ago=minutes_ago)

        rows, has_more = Sidebar(member_user, viewing=oldest.thread_id).session_rows()

        assert len(rows) == 9
        assert rows[-1].thread_id == oldest.thread_id
        assert rows[-1].active is True
        assert has_more is True

    def test_viewing_someone_elses_session_adds_nothing(self, member_user, admin_user):
        for minutes_ago in range(SESSIONS_PAGE):
            make_session(member_user, minutes_ago=minutes_ago)
        theirs = make_session(admin_user)

        rows, _ = Sidebar(member_user, viewing=theirs.thread_id).session_rows()

        assert len(rows) == 8
        assert not any(row.active for row in rows)

    @pytest.mark.parametrize("count", [3, 9])
    def test_query_count_is_fixed(self, member_user, django_assert_max_num_queries, count):
        sessions = [make_session(member_user, minutes_ago=minutes_ago) for minutes_ago in range(count)]
        for session in sessions:
            make_run(session, RunStatus.RUNNING)

        with django_assert_max_num_queries(1):
            Sidebar(member_user).session_rows()
        with django_assert_max_num_queries(2):
            Sidebar(member_user, viewing=sessions[-1].thread_id).session_rows()


@pytest.mark.django_db
class TestScheduleRows:
    def test_lists_owned_and_subscribed_enabled_schedules(self, member_user, admin_user):
        make_schedule(member_user, name="Own")
        make_schedule(admin_user, name="Subscribed").subscribers.add(member_user)
        make_schedule(admin_user, name="Theirs")

        assert {row.name for row in Sidebar(member_user).schedule_rows()} == {"Own", "Subscribed"}

    def test_schedule_with_several_subscribers_is_listed_once(self, member_user, admin_user):
        shared = make_schedule(member_user)
        shared.subscribers.add(admin_user, member_user)

        assert [row.pk for row in Sidebar(member_user).schedule_rows()] == [shared.pk]

    def test_admin_lists_only_their_own(self, admin_user, member_user):
        make_schedule(member_user, name="Member's")
        make_schedule(admin_user, name="Admin's")

        assert [row.name for row in Sidebar(admin_user).schedule_rows()] == ["Admin's"]

    @pytest.mark.parametrize(
        ("fired_days_ago", "listed"), [(FIRED_WITHIN.days - 1, True), (FIRED_WITHIN.days + 1, False), (None, False)]
    )
    def test_disabled_schedule_shows_for_14_days_after_firing(self, member_user, fired_days_ago, listed):
        last_run_at = None if fired_days_ago is None else timezone.now() - timedelta(days=fired_days_ago)
        make_schedule(member_user, is_enabled=False, last_run_at=last_run_at)

        assert bool(Sidebar(member_user).schedule_rows()) is listed

    def test_orders_next_run_first_then_most_recently_fired(self, member_user):
        now = timezone.now()
        make_schedule(member_user, name="A", next_run_at=now + timedelta(hours=6))
        make_schedule(member_user, name="B", next_run_at=now + timedelta(hours=1))
        make_schedule(member_user, name="C", is_enabled=False, last_run_at=now - timedelta(days=1))
        make_schedule(member_user, name="D", is_enabled=False, last_run_at=now - timedelta(days=2))

        assert [row.name for row in Sidebar(member_user).schedule_rows()] == ["B", "A", "C", "D"]

    def test_caps_at_ten(self, member_user):
        for index in range(SCHEDULES_LIMIT + 1):
            make_schedule(member_user, name=f"S{index}")

        assert len(Sidebar(member_user).schedule_rows()) == SCHEDULES_LIMIT

    def test_all_runs_filters_the_sessions_page_by_schedule(self, member_user):
        schedule = make_schedule(member_user)

        (row,) = Sidebar(member_user).schedule_rows()

        assert row.all_runs_url == reverse("session_list") + f"?schedule={schedule.pk}"

    def test_one_firing_per_batch_with_repo_count(self, member_user):
        schedule = make_schedule(member_user)
        batch_id, _ = make_firing(schedule, RunStatus.SUCCESSFUL, RunStatus.SUCCESSFUL, RunStatus.SUCCESSFUL)

        ((firing,),) = [row.firings for row in Sidebar(member_user).schedule_rows()]

        assert firing.batch_id == str(batch_id)
        assert firing.repo_count == 3
        assert firing.url == reverse("session_list") + f"?batch={batch_id}"

    def test_single_session_firing_links_the_session(self, member_user):
        _, (session,) = make_firing(make_schedule(member_user), RunStatus.SUCCESSFUL)

        ((firing,),) = [row.firings for row in Sidebar(member_user).schedule_rows()]

        assert firing.repo_count == 1
        assert firing.url == reverse("session_detail", kwargs={"thread_id": session.thread_id})

    def test_a_session_with_two_runs_of_one_firing_counts_once(self, member_user):
        batch_id, (session,) = make_firing(make_schedule(member_user), RunStatus.FAILED, minutes_ago=20)
        make_run(session, RunStatus.SUCCESSFUL, minutes_ago=10, batch_id=batch_id)

        ((firing,),) = [row.firings for row in Sidebar(member_user).schedule_rows()]

        assert firing.repo_count == 1
        assert firing.url == reverse("session_detail", kwargs={"thread_id": session.thread_id})

    def test_firing_status_is_the_worst(self, member_user):
        schedule = make_schedule(member_user)
        make_firing(schedule, RunStatus.SUCCESSFUL, RunStatus.RUNNING, RunStatus.FAILED, minutes_ago=60)
        make_firing(schedule, RunStatus.SUCCESSFUL, RunStatus.RUNNING, minutes_ago=10)

        (row,) = Sidebar(member_user).schedule_rows()

        assert [firing.status for firing in row.firings] == [RunStatus.RUNNING, RunStatus.FAILED]

    def test_later_chat_run_does_not_make_a_second_firing(self, member_user):
        _, (session,) = make_firing(make_schedule(member_user), RunStatus.SUCCESSFUL, minutes_ago=30)
        api_batch = uuid.uuid4()
        make_run(session, RunStatus.SUCCESSFUL, minutes_ago=20, trigger_type=SessionOrigin.API_JOB, batch_id=api_batch)
        make_run(session, RunStatus.RUNNING, minutes_ago=10, trigger_type=SessionOrigin.CHAT)

        ((firing,),) = [row.firings for row in Sidebar(member_user).schedule_rows()]

        assert firing.status == RunStatus.RUNNING

    def test_keeps_three_newest_firings(self, member_user):
        schedule = make_schedule(member_user)
        sessions = [make_firing(schedule, RunStatus.SUCCESSFUL, minutes_ago=m)[1][0] for m in (50, 40, 30, 20, 10)]

        (row,) = Sidebar(member_user).schedule_rows()

        newest = reversed(sessions[-FIRINGS_PER_SCHEDULE:])
        assert [firing.url for firing in row.firings] == [
            reverse("session_detail", kwargs={"thread_id": session.thread_id}) for session in newest
        ]

    def test_archived_sessions_leave_a_schedule_without_firings(self, member_user):
        _, (session,) = make_firing(make_schedule(member_user), RunStatus.FAILED)
        Session.objects.filter(pk=session.pk).update(archived_at=timezone.now())

        (row,) = Sidebar(member_user).schedule_rows()

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
        schedule = make_schedule(member_user)
        make_firing(schedule, RunStatus.FAILED, minutes_ago=60)
        make_firing(schedule, latest, minutes_ago=10)

        (row,) = Sidebar(member_user).schedule_rows()

        assert row.starts_open is starts_open

    def test_viewed_scheduled_session_opens_its_schedule(self, member_user):
        schedule = make_schedule(member_user)
        _, (viewed,) = make_firing(schedule, RunStatus.SUCCESSFUL, minutes_ago=50)
        for minutes_ago in (40, 30, 20, 10):
            make_firing(schedule, RunStatus.SUCCESSFUL, minutes_ago=minutes_ago)

        (row,) = Sidebar(member_user, viewing=viewed.thread_id).schedule_rows()

        assert row.starts_open is True
        assert [firing.active for firing in row.firings] == [False, False, False, True]

    def test_viewed_session_among_the_newest_is_not_added_twice(self, member_user):
        schedule = make_schedule(member_user)
        for minutes_ago in (40, 30, 20):
            make_firing(schedule, RunStatus.SUCCESSFUL, minutes_ago=minutes_ago)
        _, (viewed,) = make_firing(schedule, RunStatus.SUCCESSFUL, minutes_ago=10)

        (row,) = Sidebar(member_user, viewing=viewed.thread_id).schedule_rows()

        assert [firing.active for firing in row.firings] == [True, False, False]

    @pytest.mark.parametrize(("latest", "status"), [(RunStatus.SUCCESSFUL, ""), (RunStatus.RUNNING, RunStatus.RUNNING)])
    def test_status_only_for_state_word_statuses(self, member_user, latest, status):
        make_firing(make_schedule(member_user), latest)

        (row,) = Sidebar(member_user).schedule_rows()

        assert row.status == status

    def test_reads_only_each_schedules_newest_sessions(self, member_user, mocker):
        mocker.patch("dashboard.sidebar.NEWEST_SESSIONS", 2)
        schedule = make_schedule(member_user)
        for minutes_ago in (40, 30, 20, 10):
            make_firing(schedule, RunStatus.SUCCESSFUL, minutes_ago=minutes_ago)

        (row,) = Sidebar(member_user).schedule_rows()

        assert len(row.firings) == 2

    def test_viewed_firing_past_the_newest_sessions_shows_whole(self, member_user, django_assert_max_num_queries):
        schedule = make_schedule(member_user)
        statuses = [RunStatus.SUCCESSFUL] * MAX_REPOS_PER_BATCH
        _, (viewed, *_) = make_firing(schedule, *statuses, minutes_ago=40)
        for minutes_ago in (30, 20, 10):
            make_firing(schedule, *statuses, minutes_ago=minutes_ago)

        with django_assert_max_num_queries(3):
            (row,) = Sidebar(member_user, viewing=viewed.thread_id).schedule_rows()

        assert [firing.repo_count for firing in row.firings] == [MAX_REPOS_PER_BATCH] * (FIRINGS_PER_SCHEDULE + 1)
        assert [firing.active for firing in row.firings] == [False, False, False, True]

    def test_query_count_is_fixed(self, member_user, django_assert_max_num_queries):
        make_firing(make_schedule(member_user), RunStatus.SUCCESSFUL)
        with django_assert_max_num_queries(3):
            Sidebar(member_user).schedule_rows()

        for index in range(SCHEDULES_LIMIT - 1):
            schedule = make_schedule(member_user, name=f"S{index}")
            for minutes_ago in range(FIRINGS_PER_SCHEDULE + 1):
                _, sessions = make_firing(schedule, RunStatus.FAILED, RunStatus.SUCCESSFUL, minutes_ago=minutes_ago)
        with django_assert_max_num_queries(3):
            Sidebar(member_user, viewing=sessions[0].thread_id).schedule_rows()

    def test_sidebar_lists_combines_both(self, member_user):
        own = make_session(member_user)
        schedule = make_schedule(member_user)

        lists = Sidebar(member_user).lists()

        assert isinstance(lists, SidebarLists)
        assert [row.thread_id for row in lists.sessions] == [own.thread_id]
        assert lists.has_more is False
        assert [row.pk for row in lists.schedules] == [schedule.pk]

    @pytest.mark.parametrize(
        ("asked", "limit"), [(1, SESSIONS_PAGE), (SESSIONS_PAGE * 2, SESSIONS_PAGE * 2), (9999, SESSIONS_MAX)]
    )
    def test_sidebar_lists_clamps_the_limit(self, member_user, asked, limit):
        assert Sidebar(member_user).lists(limit=asked).limit == limit


class TestNextLimit:
    @pytest.mark.parametrize(
        ("limit", "next_limit"),
        [
            (SESSIONS_PAGE, SESSIONS_PAGE * 2),
            (SESSIONS_MAX - SESSIONS_PAGE, SESSIONS_MAX),
            (SESSIONS_MAX - 1, SESSIONS_MAX),
            (SESSIONS_MAX, None),
        ],
    )
    def test_steps_a_page_and_stops_at_the_cap(self, limit, next_limit):
        assert SidebarLists(limit=limit).next_limit == next_limit
