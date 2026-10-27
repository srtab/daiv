import uuid
from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

import pytest
from dashboard.views import BOT_USERNAME_CACHE_KEY, INBOX_LIMIT
from notifications.choices import EnvelopeStatus
from notifications.models import RunEnvelope
from sessions.models import Run, RunStatus, Session, SessionOrigin, WatchState

from codebase.base import GitPlatform
from codebase.clients import RepoClient
from codebase.conf import settings as codebase_settings
from schedules.models import DispatchError, Frequency, ScheduledJob


@pytest.fixture(autouse=True)
def _forget_bot_username():
    cache.delete(BOT_USERNAME_CACHE_KEY)


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

    def test_defaults_to_seven_days(self, member_client):
        response = member_client.get(reverse("dashboard"))

        assert response.context["current_period"] == "7d"


@pytest.mark.django_db
class TestHomeTitle:
    def test_page_is_called_home(self, member_client):
        content = member_client.get(reverse("dashboard")).content.decode()

        assert "<title>Home — DAIV</title>" in content
        assert ">Home</h1>" in content


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


def _add_run(session, status, *, days_ago=0, trigger_type=None, prompt="", **fields):
    run = Run.objects.create(
        session=session,
        status=status,
        trigger_type=trigger_type or session.origin,
        repo_id=session.repo_id,
        user=session.user,
        prompt=prompt,
        **fields,
    )
    Run.objects.filter(pk=run.pk).update(created_at=_local_noon(days_ago), finished_at=_local_noon(days_ago))
    Session.objects.filter(pk=session.pk).update(last_active_at=_local_noon(days_ago))
    return run


def _classified_session(user, envelope_status, *, days_ago=0):
    session = Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.SCHEDULE, repo_id="daiv/test", user=user, title="Scan"
    )
    run = _add_run(session, RunStatus.SUCCESSFUL, days_ago=days_ago)
    RunEnvelope.objects.create(run=run, status=envelope_status)
    return session


def _failing_schedule(user, **fields):
    defaults = {
        "name": "Nightly audit",
        "prompt": "p",
        "repos": [{"repo_id": "daiv/test", "ref": ""}],
        "frequency": Frequency.DAILY,
        "time": "09:00",
        "is_enabled": True,
        "failing_since": _local_noon(1),
        "dispatch_error": DispatchError.UNEXPECTED,
    }
    return ScheduledJob.objects.create(user=user, **(defaults | fields))


def _inbox(client):
    return client.get(reverse("dashboard")).context["inbox"]


def _keys(inbox):
    return [item.outcome.key for item in inbox["items"]]


@pytest.mark.django_db
class TestNeedsYouInbox:
    def test_lists_every_source_worst_first(self, member_user, member_client):
        _classified_session(member_user, EnvelopeStatus.NEEDS_ATTENTION)
        _classified_session(member_user, EnvelopeStatus.FOUND_ISSUES)
        _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.WAITING_INPUT)
        watched = _make_session(member_user, SessionOrigin.MR_WEBHOOK, days_ago=2)
        Session.objects.filter(pk=watched.pk).update(watch_state=WatchState.EXHAUSTED)
        _failing_schedule(member_user)
        _make_session(member_user, SessionOrigin.UI_JOB, status=RunStatus.FAILED)
        _make_session(member_user, SessionOrigin.CHAT)

        inbox = _inbox(member_client)

        assert _keys(inbox) == [
            "failed",
            "cant-run",
            "ci-still-failing",
            "needs-input",
            "found-issues",
            "needs-attention",
        ]
        assert inbox["total"] == 6

    def test_schedule_links_to_its_page_and_says_why(self, member_user, member_client):
        schedule = _failing_schedule(member_user)

        (item,) = _inbox(member_client)["items"]

        assert item.url == reverse("schedule_update", kwargs={"pk": schedule.pk})
        assert item.title == "Nightly audit"
        assert item.detail == schedule.dispatch_error_message

    def test_a_newer_run_resolves_the_session(self, member_user, member_client):
        answered = _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.WAITING_INPUT, days_ago=1)
        _add_run(answered, RunStatus.RUNNING)
        fixed = _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.FAILED, days_ago=1)
        _add_run(fixed, RunStatus.SUCCESSFUL)
        reclassified = _classified_session(member_user, EnvelopeStatus.FOUND_ISSUES, days_ago=1)
        _add_run(reclassified, RunStatus.SUCCESSFUL)

        assert _inbox(member_client)["items"] == []

    def test_a_retry_resolves_the_failed_session(self, member_user, member_client):
        failed = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="daiv/test", user=member_user
        )
        _add_run(failed, RunStatus.FAILED, days_ago=1, prompt="Fix the flaky test")
        retry = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="daiv/test", user=member_user
        )
        _add_run(retry, RunStatus.RUNNING, prompt="Fix the flaky test")

        assert _inbox(member_client)["items"] == []

    def test_a_different_prompt_is_not_a_retry(self, member_user, member_client):
        failed = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="daiv/test", user=member_user
        )
        _add_run(failed, RunStatus.FAILED, days_ago=1, prompt="Fix the flaky test")
        other = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="daiv/test", user=member_user
        )
        _add_run(other, RunStatus.SUCCESSFUL, prompt="Bump the dependencies")

        assert _keys(_inbox(member_client)) == ["failed"]

    def test_a_run_you_cancelled_does_not_need_you(self, member_user, member_client):
        session = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="daiv/test", user=member_user
        )
        _add_run(session, RunStatus.FAILED, cancel_requested_at=timezone.now())

        assert _inbox(member_client)["items"] == []

    def test_findings_and_a_given_up_watch_age_out_after_a_week(self, member_user, member_client):
        _classified_session(member_user, EnvelopeStatus.FOUND_ISSUES, days_ago=6)
        _classified_session(member_user, EnvelopeStatus.NEEDS_ATTENTION, days_ago=8)
        stale_watch = _make_session(member_user, SessionOrigin.MR_WEBHOOK, days_ago=8)
        Session.objects.filter(pk=stale_watch.pk).update(watch_state=WatchState.EXHAUSTED)
        _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.FAILED, days_ago=60)

        assert _keys(_inbox(member_client)) == ["failed", "found-issues"]

    def test_all_clear_and_schedules_that_will_not_run_stay_out(self, member_user, member_client):
        _classified_session(member_user, EnvelopeStatus.ALL_CLEAR)
        _failing_schedule(member_user, is_enabled=False)
        _failing_schedule(member_user, failing_since=None, dispatch_error="")

        assert _inbox(member_client)["items"] == []

    def test_a_teammates_items_do_not_appear(self, admin_user, admin_client, member_user):
        _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.FAILED)
        _classified_session(member_user, EnvelopeStatus.FOUND_ISSUES)
        _failing_schedule(member_user)

        inbox = _inbox(admin_client)

        assert inbox["items"] == []
        assert inbox["total"] == 0

    def test_caps_the_list_and_links_to_the_lists_it_came_from(self, member_user, member_client):
        for days_ago in range(INBOX_LIMIT + 2):
            _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.FAILED, days_ago=days_ago)
        _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.WAITING_INPUT)

        inbox = _inbox(member_client)

        assert len(inbox["items"]) == INBOX_LIMIT
        assert inbox["total"] == INBOX_LIMIT + 3
        assert _keys(inbox) == ["failed"] * INBOX_LIMIT
        assert [item.at for item in inbox["items"]] == sorted((item.at for item in inbox["items"]), reverse=True)
        urls = [link["url"] for link in inbox["see_all"]]
        assert urls == [
            f"{reverse('session_list')}?scope=mine&status=FAILED",
            f"{reverse('session_list')}?scope=mine&status=WAITING_INPUT",
        ]
        assert _list_count(member_client, urls[0]) == INBOX_LIMIT + 2

    def test_empty_state(self, member_client):
        content = member_client.get(reverse("dashboard")).content.decode()

        assert "Nothing needs you" in content


@pytest.mark.django_db
class TestRecentSessions:
    def test_shows_your_five_newest(self, admin_user, member_user, member_client):
        mine = [_make_session(member_user, SessionOrigin.CHAT, days_ago=days_ago) for days_ago in range(6)]
        _make_session(admin_user, SessionOrigin.CHAT)

        response = member_client.get(reverse("dashboard"))

        assert [s.pk for s in response.context["recent_sessions"]] == [s.pk for s in mine[:5]]
        assert response.context["my_sessions_url"] == f"{reverse('session_list')}?scope=mine"

    def test_hidden_when_you_have_none(self, admin_user, member_client):
        _make_session(admin_user, SessionOrigin.CHAT)

        response = member_client.get(reverse("dashboard"))

        assert response.context["recent_sessions"] == []
        assert 'data-testid="recent-sessions"' not in response.content.decode()


@pytest.mark.django_db
class TestWaysToUse:
    def test_gitlab_instance(self, member_client):
        content = member_client.get(reverse("dashboard")).content.decode()

        assert "On GitLab" in content
        for label in ("daiv", "daiv-auto", "daiv-max"):
            assert f">{label}</code>" in content
        assert "@test-user" in content
        assert "@test-user /plan" in content
        assert "merge request comment" in content
        assert "claude mcp add daiv --transport http http://testserver/mcp/" in content
        assert reverse("api_keys") in content
        assert reverse("session_new_chat") in content
        assert reverse("runs:agent_run_new") in content

    def test_github_instance(self, member_client):
        with patch.object(codebase_settings, "CLIENT", GitPlatform.GITHUB):
            content = member_client.get(reverse("dashboard")).content.decode()

        assert "On GitHub" in content
        assert "pull request comment" in content
        assert "merge request comment" not in content

    def test_mcp_url_follows_the_host_it_is_served_on(self, member_client):
        response = member_client.get(reverse("dashboard"), secure=True, HTTP_HOST="daiv.example.com")

        assert response.context["ways"]["mcp_url"] == "https://daiv.example.com/mcp/"

    def test_falls_back_to_the_label_when_the_platform_is_unreachable(self, member_client):
        RepoClient.create_instance.side_effect = RuntimeError("platform down")

        ways = member_client.get(reverse("dashboard")).context["ways"]

        assert ways["bot_mention"] == "@daiv"

    def test_prominent_until_you_have_a_session(self, member_user, member_client):
        before = member_client.get(reverse("dashboard")).content.decode()
        _make_session(member_user, SessionOrigin.CHAT)
        after = member_client.get(reverse("dashboard")).content.decode()

        assert "Start work from wherever you already are" in before
        assert "Start work from wherever you already are" not in after
        assert 'data-testid="ways-to-use"' in after
