import uuid
from datetime import datetime, time, timedelta

from django.test import Client
from django.urls import reverse
from django.utils import timezone

import pytest
from sessions.models import Run, RunStatus, Session, SessionOrigin


def _local_noon(days_ago: int) -> datetime:
    return timezone.make_aware(datetime.combine(timezone.localdate() - timedelta(days=days_ago), time(12)))


def _make_run(user, trigger_type):
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=trigger_type, repo_id="daiv/test", user=user)
    return Run.objects.create(
        session=session, status=RunStatus.SUCCESSFUL, trigger_type=trigger_type, repo_id="daiv/test", user=user
    )


@pytest.mark.django_db
class TestHomepage:
    def test_anonymous_sees_the_homepage(self):
        response = Client().get(reverse("homepage"))
        assert response.status_code == 200
        assert "dashboard/homepage.html" in [t.name for t in response.templates]

    def test_signed_in_user_is_sent_to_the_dashboard(self, member_client):
        response = member_client.get(reverse("homepage"))
        assert response.status_code == 302
        assert response.url == reverse("dashboard")


@pytest.mark.django_db
class TestDashboardPeriod:
    def test_seven_days_covers_today_and_the_six_days_before(self, member_user, member_client):
        inside = _make_run(member_user, SessionOrigin.API_JOB)
        outside = _make_run(member_user, SessionOrigin.API_JOB)
        Run.objects.filter(pk=inside.pk).update(created_at=_local_noon(6))
        Run.objects.filter(pk=outside.pk).update(created_at=_local_noon(7))

        response = member_client.get(reverse("dashboard"), {"period": "7d"})

        assert response.context["activity"]["total"] == 1


def _make_session(user, origin, *, status=RunStatus.SUCCESSFUL, days_ago=0):
    session = Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=origin, repo_id="daiv/test", user=user, last_active_at=_local_noon(days_ago)
    )
    Run.objects.create(session=session, status=status, trigger_type=origin, repo_id="daiv/test", user=user)
    return session


def _add_run(session, *, days_ago):
    return Run.objects.create(
        session=session,
        status=RunStatus.SUCCESSFUL,
        trigger_type=session.origin,
        repo_id="daiv/test",
        user=session.user,
        created_at=_local_noon(days_ago),
    )


def _list_count(client, url: str) -> int:
    return client.get(url).context["paginator"].count


@pytest.mark.django_db
class TestDashboardSessionBreakdown:
    def test_every_count_matches_the_list_its_link_opens(self, member_user, member_client):
        _make_session(member_user, SessionOrigin.CHAT, days_ago=6)
        _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.FAILED)
        _make_session(member_user, SessionOrigin.UI_JOB, days_ago=2)
        _make_session(member_user, SessionOrigin.ISSUE_WEBHOOK, status=RunStatus.FAILED, days_ago=1)
        _make_session(member_user, SessionOrigin.API_JOB, days_ago=7)
        _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.FAILED, days_ago=8)

        activity = member_client.get(reverse("dashboard"), {"period": "7d"}).context["activity"]

        values = {s["label"]: s["value"] for s in activity["segments"]}
        assert values == {"Chat": 2, "UI Run": 1, "Issue Webhook": 1}
        assert activity["sessions_failed"] == 2
        for segment in activity["segments"]:
            assert _list_count(member_client, segment["url"]) == segment["value"], segment["label"]
        assert _list_count(member_client, activity["sessions_failed_url"]) == activity["sessions_failed"]
        assert _list_count(member_client, activity["sessions_url"]) == activity["sessions_total"] == 4

    def test_a_session_counts_once_however_many_runs_it_holds(self, member_user, member_client):
        session = _make_session(member_user, SessionOrigin.MR_WEBHOOK)
        Run.objects.create(
            session=session,
            status=RunStatus.FAILED,
            trigger_type=SessionOrigin.CHAT,
            repo_id="daiv/test",
            user=member_user,
        )

        activity = member_client.get(reverse("dashboard")).context["activity"]

        assert [(s["label"], s["value"]) for s in activity["segments"]] == [("MR/PR Webhook", 1)]
        assert activity["sessions_failed"] == 1

    def test_home_skips_the_comparison_and_charts(self, member_user, member_client):
        _make_session(member_user, SessionOrigin.CHAT)

        activity = member_client.get(reverse("dashboard"), {"period": "7d"}).context["activity"]

        assert activity["total_change"] is None
        assert activity["chart"] is None
        assert activity["segments"][0]["spark"] == []

    def test_links_carry_the_period(self, member_user, member_client):
        _make_session(member_user, SessionOrigin.SCHEDULE)

        activity = member_client.get(reverse("dashboard"), {"period": "30d"}).context["activity"]

        date_from = (timezone.localdate() - timedelta(days=29)).isoformat()
        expected = f"{reverse('session_list')}?scope=all&date_from={date_from}&trigger=schedule"
        assert activity["segments"][0]["url"] == expected

    def test_all_time_links_have_no_date(self, member_user, member_client):
        _make_session(member_user, SessionOrigin.SCHEDULE, days_ago=400)

        activity = member_client.get(reverse("dashboard"), {"period": "all"}).context["activity"]

        assert activity["segments"][0]["url"] == f"{reverse('session_list')}?scope=all&trigger=schedule"

    def test_counts_include_teammates_sessions_and_so_do_the_lists_they_open(
        self, admin_user, admin_client, member_user
    ):
        _make_session(admin_user, SessionOrigin.CHAT)
        _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.FAILED)

        activity = admin_client.get(reverse("dashboard")).context["activity"]

        (chat,) = activity["segments"]
        assert chat["value"] == 2
        assert _list_count(admin_client, chat["url"]) == 2
        assert _list_count(admin_client, activity["sessions_failed_url"]) == activity["sessions_failed"] == 1
        assert _list_count(admin_client, reverse("session_list")) == 1


def test_every_origin_has_a_segment_colour():
    from dashboard.insights import ORIGIN_SEGMENT_CSS

    assert set(ORIGIN_SEGMENT_CSS) == set(SessionOrigin)


def _insights(client, **params):
    return client.get(reverse("insights"), params).context


def _merge(repo_id, *, daiv_commits=0, days_ago=0, iid=None):
    from codebase.models import MergeMetric

    return MergeMetric.objects.create(
        repo_id=repo_id,
        merge_request_iid=iid or MergeMetric.objects.count() + 1,
        merged_at=_local_noon(days_ago),
        target_branch="main",
        source_branch="feature",
        platform="gitlab",
        total_commits=4,
        daiv_commits=daiv_commits,
        lines_added=10,
        lines_removed=3,
    )


@pytest.mark.django_db
class TestInsightsPage:
    def test_renders_with_seven_days_by_default(self, member_client):
        response = member_client.get(reverse("insights"))

        assert response.status_code == 200
        assert response.context["current_period"] == "7d"
        assert "<title>Insights — DAIV</title>" in response.content.decode()

    def test_compares_with_the_seven_days_before(self, member_user, member_client):
        for days_ago in (0, 6):
            _make_session(member_user, SessionOrigin.CHAT, days_ago=days_ago)
        Run.objects.filter(session__last_active_at=_local_noon(6)).update(created_at=_local_noon(6))
        earlier = _make_session(member_user, SessionOrigin.CHAT, days_ago=9)
        Run.objects.filter(session=earlier).update(created_at=_local_noon(9))

        context = _insights(member_client)

        assert context["activity"]["total"] == 2
        assert context["activity"]["total_change"] == {"direction": "up", "amount": "1", "was": "1"}
        chat = context["breakdown"]["segments"][0]
        assert (chat["value"], chat["change"]["was"], chat["change"]["pct"]) == (2, "1", "+100%")

    def test_a_session_active_in_both_periods_counts_in_both(self, member_user, member_client):
        session = _make_session(member_user, SessionOrigin.CHAT)
        _add_run(session, days_ago=9)

        (chat,) = _insights(member_client)["breakdown"]["segments"]

        assert (chat["value"], chat["change"]["was"]) == (1, "1")

    def test_a_sub_second_change_in_duration_is_no_change(self, member_user, member_client):
        current = _make_session(member_user, SessionOrigin.CHAT)
        earlier = _make_session(member_user, SessionOrigin.CHAT, days_ago=9)
        then, now = _local_noon(9), timezone.now()
        Run.objects.filter(session=earlier).update(
            created_at=then, started_at=then, finished_at=then + timedelta(seconds=62)
        )
        Run.objects.filter(session=current).update(started_at=now, finished_at=now + timedelta(seconds=62.4))

        change = _insights(member_client)["activity"]["avg_duration_change"]

        assert change["direction"] == "flat"

    def test_all_time_has_no_comparison(self, member_user, member_client):
        _make_session(member_user, SessionOrigin.CHAT)

        context = _insights(member_client, period="all")

        assert context["previous_window"] is None
        assert context["activity"]["total_change"] is None
        assert context["breakdown"]["segments"][0]["change"] is None

    def test_every_count_matches_the_list_its_link_opens(self, member_user, member_client):
        _make_session(member_user, SessionOrigin.CHAT, days_ago=6)
        _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.FAILED)
        _make_session(member_user, SessionOrigin.ISSUE_WEBHOOK, days_ago=8)

        breakdown = _insights(member_client)["breakdown"]

        for segment in breakdown["segments"]:
            assert _list_count(member_client, segment["url"]) == segment["value"], segment["label"]
        assert _list_count(member_client, breakdown["sessions_url"]) == breakdown["sessions_total"] == 2
        assert _list_count(member_client, breakdown["sessions_failed_url"]) == breakdown["sessions_failed"] == 1

    def test_runs_chart_has_a_column_per_day_by_outcome(self, member_user, member_client):
        changed = _make_session(member_user, SessionOrigin.UI_JOB, days_ago=2)
        Run.objects.filter(session=changed).update(code_changes=True, created_at=_local_noon(2))
        _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.FAILED)

        chart = _insights(member_client)["activity"]["chart"]

        assert [c["label"] for c in chart["columns"]][-1] == "Today"
        assert len(chart["columns"]) == 7
        assert {item["label"]: item["count"] for item in chart["legend"]} == {
            "Changed code": 1,
            "Done, no changes": 0,
            "Needs input": 0,
            "Failed": 1,
        }
        two_days_ago = chart["columns"][4]
        assert [(s["label"], s["count"]) for s in two_days_ago["segments"]] == [("Changed code", 1)]
        assert _list_count(member_client, two_days_ago["url"]) == 1

    def test_success_rate_change_is_in_points_and_was_a_rate(self, member_user, member_client):
        _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.FAILED)
        earlier = _make_session(member_user, SessionOrigin.CHAT, days_ago=9)
        Run.objects.filter(session=earlier).update(created_at=_local_noon(9))

        change = _insights(member_client)["activity"]["success_rate_change"]

        assert change == {"direction": "down", "amount": "100 pts", "was": "100%"}

    def test_no_runs_shows_an_empty_state_not_a_blank_chart(self, member_client):
        response = member_client.get(reverse("insights"))

        assert response.context["activity"]["chart"]["empty"] is True
        content = response.content.decode()
        assert 'data-testid="runs-chart-empty"' in content
        assert 'data-testid="runs-chart"' not in content

    def test_today_has_no_over_time_column(self, member_user, member_client):
        _make_session(member_user, SessionOrigin.CHAT)

        response = member_client.get(reverse("insights"), {"period": "today"})

        assert response.context["breakdown"]["has_sparks"] is False
        assert "Over time" not in response.content.decode()

    def test_one_day_has_no_chart(self, member_user, member_client):
        _make_session(member_user, SessionOrigin.CHAT)

        assert _insights(member_client, period="today")["activity"]["chart"] is None

    def test_long_periods_group_by_week(self, member_user, member_client):
        _make_session(member_user, SessionOrigin.CHAT, days_ago=20)

        chart = _insights(member_client, period="30d")["activity"]["chart"]

        assert 5 <= len(chart["columns"]) <= 6

    def test_sparkline_counts_a_session_on_each_day_it_ran(self, member_user, member_client):
        session = _make_session(member_user, SessionOrigin.CHAT)
        _add_run(session, days_ago=1)

        (chat,) = _insights(member_client)["breakdown"]["segments"]

        assert [bar["count"] for bar in chat["spark"]] == [0, 0, 0, 0, 0, 1, 1]


@pytest.mark.django_db
class TestInsightsVelocity:
    def test_a_member_sees_merges_on_repositories_they_can_read(self, member_user, member_client):
        from tests.unit_tests.sessions.conftest import grant_read_access

        grant_read_access(member_user, "daiv/readable")
        _merge("daiv/readable", daiv_commits=2)
        _merge("daiv/readable")
        _merge("daiv/hidden", daiv_commits=1)

        velocity = _insights(member_client)["velocity"]

        assert velocity["total_merges"] == 2
        assert velocity["daiv_merges"] == 1
        assert velocity["daiv_merges_pct"] == "50%"

    def test_a_member_without_access_sees_no_section(self, member_client):
        _merge("daiv/hidden")

        response = member_client.get(reverse("insights"))

        assert response.context["velocity"] is None
        assert 'data-testid="insights-velocity"' not in response.content.decode()

    def test_an_admin_sees_every_repository(self, admin_client):
        _merge("daiv/one", daiv_commits=1)
        _merge("daiv/two")

        velocity = _insights(admin_client)["velocity"]

        assert velocity["total_merges"] == 2

    def test_merges_chart_splits_by_daiv_involvement(self, admin_client):
        _merge("daiv/one", daiv_commits=1, days_ago=1)
        _merge("daiv/one", days_ago=1)
        _merge("daiv/one", days_ago=10)

        chart = _insights(admin_client)["velocity"]["chart"]

        assert {item["label"]: item["count"] for item in chart["legend"]} == {"Involved DAIV": 1, "People only": 1}
        assert chart["columns"][5]["total"] == 2
