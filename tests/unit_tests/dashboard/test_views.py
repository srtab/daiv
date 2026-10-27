import uuid
from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

import pytest
from dashboard.views import BOT_USERNAME_CACHE_KEY, BOT_USERNAME_PENDING_KEY, INBOX_LIMIT
from notifications.choices import EnvelopeStatus
from notifications.models import RunEnvelope
from sessions.models import Run, RunStatus, Session, SessionOrigin, WatchState

from codebase.base import GitPlatform
from codebase.clients import RepoClient
from codebase.conf import settings as codebase_settings
from core.conf import settings as core_settings
from schedules.models import DispatchError, Frequency, ScheduledJob


@pytest.fixture(autouse=True)
def _forget_bot_username():
    cache.delete_many([BOT_USERNAME_CACHE_KEY, BOT_USERNAME_PENDING_KEY])


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
class TestHomeTitle:
    def test_page_is_called_home(self, member_client):
        content = member_client.get(reverse("dashboard")).content.decode()

        assert "<title>Home — DAIV</title>" in content
        assert ">Home</h1>" in content

    def test_team_statistics_live_on_insights(self, member_client):
        response = member_client.get(reverse("dashboard"))

        assert "activity" not in response.context
        assert "periods" not in response.context
        assert reverse("insights") in response.content.decode()


def _make_session(user, origin, *, status=RunStatus.SUCCESSFUL, days_ago=0):
    session = Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=origin, repo_id="daiv/test", user=user, last_active_at=_local_noon(days_ago)
    )
    Run.objects.create(session=session, status=status, trigger_type=origin, repo_id="daiv/test", user=user)
    return session


def _mcp_url() -> str:
    return f"{str(core_settings.EXTERNAL_URL).rstrip('/')}/mcp/"


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


def test_every_origin_has_a_segment_colour():
    from dashboard.insights import ORIGIN_SEGMENT_CSS

    assert set(ORIGIN_SEGMENT_CSS) == set(SessionOrigin)


def _add_latest_run(session, status, *, days_ago=0, trigger_type=None, prompt="", **fields):
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
    run = _add_latest_run(session, RunStatus.SUCCESSFUL, days_ago=days_ago)
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
        _add_latest_run(answered, RunStatus.RUNNING)
        fixed = _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.FAILED, days_ago=1)
        _add_latest_run(fixed, RunStatus.SUCCESSFUL)
        reclassified = _classified_session(member_user, EnvelopeStatus.FOUND_ISSUES, days_ago=1)
        _add_latest_run(reclassified, RunStatus.SUCCESSFUL)

        assert _inbox(member_client)["items"] == []

    def test_a_retry_resolves_the_failed_session(self, member_user, member_client):
        failed = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="daiv/test", user=member_user
        )
        _add_latest_run(failed, RunStatus.FAILED, days_ago=1, prompt="Fix the flaky test")
        retry = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="daiv/test", user=member_user
        )
        _add_latest_run(retry, RunStatus.RUNNING, prompt="Fix the flaky test")

        assert _inbox(member_client)["items"] == []

    def test_a_later_run_of_the_schedule_resolves_its_failed_run(self, member_user, member_client):
        schedule = _failing_schedule(member_user, failing_since=None, dispatch_error="")
        sessions = [
            Session.objects.create(
                thread_id=str(uuid.uuid4()),
                origin=SessionOrigin.SCHEDULE,
                repo_id="daiv/test",
                user=member_user,
                scheduled_job=schedule,
            )
            for _ in range(2)
        ]
        _add_latest_run(sessions[0], RunStatus.FAILED, days_ago=1, prompt="Audit")
        _add_latest_run(sessions[1], RunStatus.SUCCESSFUL, prompt="Audit")

        assert _inbox(member_client)["items"] == []

    def test_a_teammates_run_of_the_same_prompt_is_not_your_retry(self, admin_user, member_user, member_client):
        failed = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.MCP_JOB, repo_id="daiv/test", user=member_user
        )
        _add_latest_run(failed, RunStatus.FAILED, days_ago=1, prompt="Fix the failing tests")
        theirs = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.MCP_JOB, repo_id="daiv/test", user=admin_user
        )
        _add_latest_run(theirs, RunStatus.SUCCESSFUL, prompt="Fix the failing tests")

        assert _keys(_inbox(member_client)) == ["failed"]

    def test_a_teammates_question_in_a_thread_you_joined_is_not_yours(self, admin_user, member_user, member_client):
        shared = _make_session(member_user, SessionOrigin.MR_WEBHOOK, days_ago=1)
        Run.objects.create(
            session=shared,
            status=RunStatus.WAITING_INPUT,
            trigger_type=SessionOrigin.MR_WEBHOOK,
            repo_id="daiv/test",
            external_username=admin_user.username,
        )

        assert _inbox(member_client)["items"] == []

    def test_a_different_prompt_is_not_a_retry(self, member_user, member_client):
        failed = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="daiv/test", user=member_user
        )
        _add_latest_run(failed, RunStatus.FAILED, days_ago=1, prompt="Fix the flaky test")
        other = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="daiv/test", user=member_user
        )
        _add_latest_run(other, RunStatus.SUCCESSFUL, prompt="Bump the dependencies")

        assert _keys(_inbox(member_client)) == ["failed"]

    def test_a_run_you_cancelled_does_not_need_you(self, member_user, member_client):
        session = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="daiv/test", user=member_user
        )
        _add_latest_run(session, RunStatus.FAILED, cancel_requested_at=timezone.now())

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

    def test_rows_leave_the_model_to_the_full_list(self, member_user, member_client):
        session = _make_session(member_user, SessionOrigin.CHAT)
        Run.objects.filter(session=session).update(agent_model="claude-test-model")

        home = member_client.get(reverse("dashboard")).content.decode()
        listed = member_client.get(reverse("session_list")).content.decode()

        assert "claude-test-model" not in home
        assert "claude-test-model" in listed

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
        assert f"claude mcp add daiv --transport http {_mcp_url()}" in content
        assert reverse("api_keys") in content
        assert reverse("session_new_chat") in content
        assert reverse("runs:agent_run_new") in content

    def test_github_instance(self, member_client):
        with patch.object(codebase_settings, "CLIENT", GitPlatform.GITHUB):
            content = member_client.get(reverse("dashboard")).content.decode()

        assert "On GitHub" in content
        assert "pull request comment" in content
        assert "merge request comment" not in content

    def test_mcp_url_is_the_external_url_not_the_request_host(self, member_client):
        response = member_client.get(reverse("dashboard"), HTTP_HOST="daiv-web:8000")

        assert response.context["ways"]["mcp_url"] == _mcp_url()

    def test_falls_back_to_the_label_when_the_platform_is_unreachable(self, member_client):
        RepoClient.create_instance.side_effect = RuntimeError("platform down")

        ways = member_client.get(reverse("dashboard")).context["ways"]

        assert ways["bot_mention"] == "@daiv"

    def test_an_unresolved_username_is_not_looked_up_again_on_every_load(self, member_client):
        RepoClient.create_instance.side_effect = RuntimeError("platform down")
        member_client.get(reverse("dashboard"))
        RepoClient.create_instance.reset_mock()

        member_client.get(reverse("dashboard"))

        RepoClient.create_instance.assert_not_called()

    def test_prominent_until_you_have_a_session(self, member_user, member_client):
        before = member_client.get(reverse("dashboard")).content.decode()
        _make_session(member_user, SessionOrigin.CHAT)
        after = member_client.get(reverse("dashboard")).content.decode()

        assert "Start work from wherever you already are" in before
        assert "Start work from wherever you already are" not in after
        assert 'data-testid="ways-to-use"' in after


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


def _week(client):
    return client.get(reverse("dashboard")).context["your_week"]


def _week_run(session, status=RunStatus.SUCCESSFUL, *, days_ago=0, trigger_type=None, **fields):
    run = Run.objects.create(
        session=session,
        status=status,
        trigger_type=trigger_type or session.origin,
        repo_id=session.repo_id,
        user=session.user,
        **fields,
    )
    Run.objects.filter(pk=run.pk).update(created_at=_local_noon(days_ago))
    return run


def _bare_session(user, origin=SessionOrigin.CHAT, *, days_ago=0, title="Work"):
    return Session.objects.create(
        thread_id=str(uuid.uuid4()),
        origin=origin,
        repo_id="daiv/test",
        user=user,
        title=title,
        last_active_at=_local_noon(days_ago),
    )


@pytest.mark.django_db
class TestYourWeek:
    def test_counts_your_runs_by_outcome(self, member_user, member_client):
        session = _bare_session(member_user)
        _week_run(session, code_changes=True)
        _week_run(session)
        _week_run(session, RunStatus.WAITING_INPUT)
        flagged = _week_run(session, days_ago=1)
        RunEnvelope.objects.create(run=flagged, status=EnvelopeStatus.FOUND_ISSUES)
        _week_run(session, RunStatus.FAILED)
        _week_run(session, RunStatus.FAILED, cancel_requested_at=timezone.now())
        _week_run(session, RunStatus.RUNNING)

        week = _week(member_client)

        legend = {item["label"]: item["count"] for item in week["chart"]["legend"]}
        assert legend == {"Changed code": 1, "Done, no changes": 1, "Needed you": 2, "Failed": 1}
        assert (week["runs"], week["sessions"]) == (5, 1)

    def test_only_your_sessions_and_the_last_seven_days(self, admin_user, member_user, member_client):
        mine = _bare_session(member_user, days_ago=7)
        _week_run(mine, days_ago=6)
        _week_run(mine, days_ago=7)
        _week_run(_bare_session(admin_user), RunStatus.FAILED)

        week = _week(member_client)

        assert week["runs"] == 1
        assert [c["total"] for c in week["chart"]["columns"]] == [1, 0, 0, 0, 0, 0, 0]
        assert week["chart"]["columns"][-1]["label"] == "Today"

    def test_a_day_opens_your_sessions_active_that_day(self, member_user, member_client):
        session = _bare_session(member_user, days_ago=2)
        _week_run(session, days_ago=2)

        column = _week(member_client)["chart"]["columns"][4]

        assert column["total"] == 1
        assert "scope=mine" in column["url"]
        assert _list_count(member_client, column["url"]) == 1

    def test_a_day_opens_the_sessions_that_ran_that_day_even_if_active_since(self, member_user, member_client):
        session = _bare_session(member_user)
        _week_run(session, days_ago=2)
        _week_run(session)

        column = _week(member_client)["chart"]["columns"][4]

        assert _list_count(member_client, column["url"]) == 1

    def test_counts_only_the_runs_you_started_in_a_shared_thread(self, admin_user, member_user, member_client):
        session = _bare_session(member_user, SessionOrigin.MR_WEBHOOK)
        _week_run(session)
        Run.objects.create(
            session=session,
            status=RunStatus.SUCCESSFUL,
            trigger_type=SessionOrigin.MR_WEBHOOK,
            repo_id="daiv/test",
            external_username=admin_user.username,
        )

        assert _week(member_client)["runs"] == 1

    def test_one_session_is_singular(self, member_user, member_client):
        session = _bare_session(member_user)
        _week_run(session)
        _week_run(session)

        content = " ".join(member_client.get(reverse("dashboard")).content.decode().split())

        assert "in 1 session" in content
        assert "1 sessions" not in content

    def test_a_merge_request_keeps_the_url_an_older_run_has(self, member_user, member_client):
        session = _bare_session(member_user)
        _week_run(session, days_ago=1, merge_request_iid=7, merge_request_web_url="https://x/7")
        _week_run(session, merge_request_iid=7)

        assert _week(member_client)["merge_requests"][0]["url"] == "https://x/7"

    def test_merge_requests_are_listed_once_newest_first(self, member_user, member_client):
        for iid, days_ago in [(1, 5), (2, 4), (3, 3), (4, 2)]:
            session = _bare_session(member_user, title=f"Change {iid}")
            _week_run(session, days_ago=days_ago, merge_request_iid=iid, merge_request_web_url=f"https://x/{iid}")
            _week_run(session, days_ago=days_ago, merge_request_iid=iid)

        week = _week(member_client)

        assert [mr["iid"] for mr in week["merge_requests"]] == [4, 3, 2]
        assert (week["merge_requests_total"], week["merge_requests_more"]) == (4, 1)
        assert week["merge_requests"][0]["title"] == "Change 4"

    def test_started_from_groups_triggers(self, member_user, member_client):
        session = _bare_session(member_user, SessionOrigin.ISSUE_WEBHOOK)
        _week_run(session)
        _week_run(session, trigger_type=SessionOrigin.MR_WEBHOOK)
        _week_run(session, trigger_type=SessionOrigin.MCP_JOB)
        _week_run(session, trigger_type=SessionOrigin.CHAT)

        assert _week(member_client)["started_from"] == [
            {"label": "GitLab", "count": 2},
            {"label": "Your editor", "count": 1},
            {"label": "Here", "count": 1},
        ]

    def test_started_from_covers_every_trigger(self, member_user, member_client):
        session = _bare_session(member_user, SessionOrigin.API_JOB)
        for origin in SessionOrigin.values:
            _week_run(session, trigger_type=origin)

        week = _week(member_client)

        assert sum(source["count"] for source in week["started_from"]) == week["runs"] == len(SessionOrigin.values)
        assert {"label": "GitLab", "count": 3} in week["started_from"]

    def test_quiet_week_shows_a_nudge(self, member_user, member_client):
        _week_run(_bare_session(member_user, days_ago=10), days_ago=10)

        response = member_client.get(reverse("dashboard"))

        assert response.context["your_week"]["chart"]["empty"] is True
        assert 'data-testid="your-week-empty"' in response.content.decode()

    def test_hidden_when_you_have_no_sessions(self, member_client):
        response = member_client.get(reverse("dashboard"))

        assert response.context["your_week"] is None
        assert 'data-testid="your-week"' not in response.content.decode()
