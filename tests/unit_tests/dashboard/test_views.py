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
    from dashboard.views import ORIGIN_SEGMENT_CSS

    assert set(ORIGIN_SEGMENT_CSS) == set(SessionOrigin)
