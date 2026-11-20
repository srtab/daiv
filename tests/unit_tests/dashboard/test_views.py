import csv
import io
import json
import re
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

import pytest
from asgiref.sync import async_to_sync
from bs4 import BeautifulSoup
from dashboard import landing
from dashboard.comparisons import CHECKED, COMPARISONS
from dashboard.conf import DashboardSettings
from dashboard.conf import settings as dashboard_settings
from dashboard.home import INBOX_LIMIT
from dashboard.sidebar import SidebarLists
from dashboard.tasks import BOT_USERNAME_CACHE_KEY, BOT_USERNAME_PENDING_KEY
from notifications.choices import EnvelopeStatus
from notifications.models import RunEnvelope
from sessions.models import (
    EventKind,
    MergeRequestOwnership,
    MergeRequestState,
    QuietReason,
    Run,
    RunStatus,
    Session,
    SessionOrigin,
    SpendTask,
)
from sessions.spend_ledger import COLUMNS

from codebase.base import GitPlatform
from codebase.clients import RepoClient
from codebase.conf import settings as codebase_settings
from core.conf import settings as core_settings
from schedules.models import DispatchError, Frequency, ScheduledJob
from tests.unit_tests.dashboard.factories import spend_record, spend_run, spend_session
from tests.unit_tests.sessions.conftest import grant_read_access
from tests.unit_tests.sessions.inbox.conftest import make_event


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

    def test_faq_json_ld_repeats_the_visible_faq(self):
        content = Client().get(reverse("homepage")).content.decode()

        blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', content, re.DOTALL)
        faq_page = next(data for data in map(json.loads, blocks) if data["@type"] == "FAQPage")
        assert [(q["name"], q["acceptedAnswer"]["text"]) for q in faq_page["mainEntity"]] == [
            (faq.question, faq.answer) for faq in landing.FAQS
        ]
        for faq in landing.FAQS:
            assert faq.answer in content

    def test_footer_links_the_social_accounts_the_json_ld_names(self):
        content = Client().get(reverse("homepage")).content.decode()

        footer = BeautifulSoup(content, "html.parser").select_one("footer.landing-footer")
        for link in landing.SOCIAL_LINKS:
            assert "me" in footer.find("a", href=link.url)["rel"]
        blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', content, re.DOTALL)
        app = next(data for data in map(json.loads, blocks) if data["@type"] == "SoftwareApplication")
        assert app["sameAs"] == [link.url for link in landing.SOCIAL_LINKS]

    def test_canonical_points_at_daivagent_com_on_any_host(self):
        content = Client(HTTP_HOST="daiv.internal.example").get("/?utm_source=x").content.decode()

        assert '<link rel="canonical" href="https://daivagent.com/">' in content
        assert '<meta property="og:url" content="https://daivagent.com/">' in content

    def test_hero_deploy_link_is_the_quickstart(self):
        content = Client().get(reverse("homepage")).content.decode()

        assert f'href="{landing.QUICKSTART_URL}"' in content

    def test_demo_plays_muted_and_never_on_its_own(self):
        content = Client().get(reverse("homepage")).content.decode()

        assert re.search(r"<video[^>]*\bmuted\b[^>]*\bplaysinline\b[^>]*\bpreload=\"none\"", content)
        assert "autoplay" not in content
        assert '<source src="/static/dashboard/video/demo.mp4" type="video/mp4">' in content

    def test_links_every_comparison(self):
        content = Client().get(reverse("homepage")).content.decode()

        for comparison in COMPARISONS:
            assert f'href="{reverse("comparison", args=[comparison.slug])}"' in content


@pytest.mark.django_db
class TestComparison:
    @pytest.mark.parametrize("comparison", COMPARISONS, ids=lambda comparison: comparison.slug)
    def test_page_shows_both_sides_with_their_sources(self, comparison):
        response = Client().get(reverse("comparison", args=[comparison.slug]))

        assert response.status_code == 200
        content = response.content.decode()
        assert f"DAIV and {comparison.name}</h1>" in content
        assert f'<meta property="og:title" content="DAIV and {comparison.name} compared">' in content
        for row in comparison.rows:
            for source in (*row.daiv.sources, *row.theirs.sources):
                assert f'href="{source}"' in content

    def test_canonical_points_at_daivagent_com_on_any_host(self):
        path = reverse("comparison", args=[COMPARISONS[0].slug])
        content = Client(HTTP_HOST="daiv.internal.example").get(path).content.decode()

        assert f'<link rel="canonical" href="https://daivagent.com{path}">' in content

    def test_unknown_comparison_is_not_found(self):
        assert Client().get("/compare/nothing-like-this/").status_code == 404

    def test_signed_in_user_can_read_it_without_links_into_the_homepage(self, member_client):
        response = member_client.get(reverse("comparison", args=[COMPARISONS[0].slug]))

        assert response.status_code == 200
        assert f'href="{reverse("homepage")}#' not in response.content.decode()

    def test_sitemap_lists_every_comparison(self):
        content = Client().get(reverse("sitemap")).content.decode()

        for comparison in COMPARISONS:
            assert reverse("comparison", args=[comparison.slug]) in content
        assert f"<lastmod>{CHECKED:%Y-%m-%d}</lastmod>" in content

    def test_page_says_when_it_was_checked(self):
        content = Client().get(reverse("comparison", args=[COMPARISONS[0].slug])).content.decode()

        assert f"as of {CHECKED:%B %Y}." in content


@pytest.mark.django_db
def test_sitemap_lists_urls_at_daivagent_com_on_any_host():
    content = Client(HTTP_HOST="daiv.internal.example").get(reverse("sitemap")).content.decode()

    locations = re.findall(r"<loc>(.*?)</loc>", content)
    assert locations
    assert all(location.startswith("https://daivagent.com/") for location in locations)


UMAMI_WEBSITE_ID = uuid.UUID("0b6bd0c4-5d6c-4f2e-9a43-1d2e3f4a5b6c")
UMAMI_TAG = f'data-website-id="{UMAMI_WEBSITE_ID}"'
COMPARISON = ("comparison", [COMPARISONS[0].slug])


@pytest.mark.django_db
class TestUmamiAnalytics:
    @pytest.fixture
    def umami(self, monkeypatch):
        monkeypatch.setattr(dashboard_settings, "UMAMI_WEBSITE_ID", UMAMI_WEBSITE_ID)
        monkeypatch.setattr(dashboard_settings, "UMAMI_SCRIPT_URL", "https://stats.example.com/script.js")

    def test_off_unless_an_install_sets_a_website_id(self, monkeypatch):
        monkeypatch.delenv("DAIV_UMAMI_WEBSITE_ID", raising=False)
        monkeypatch.setattr(dashboard_settings, "UMAMI_WEBSITE_ID", DashboardSettings().UMAMI_WEBSITE_ID)

        assert "data-website-id" not in Client().get(reverse("homepage")).content.decode()

    @pytest.mark.usefixtures("umami")
    @pytest.mark.parametrize(("name", "args"), [("homepage", []), COMPARISON])
    def test_public_pages_load_the_tracker_for_signed_out_visitors(self, name, args):
        content = Client().get(reverse(name, args=args)).content.decode()

        assert f'<script defer src="https://stats.example.com/script.js" {UMAMI_TAG}' in content

    @pytest.mark.usefixtures("umami")
    @pytest.mark.parametrize(("name", "args"), [COMPARISON, ("dashboard", [])])
    def test_signed_in_users_are_never_tracked(self, member_client, name, args):
        response = member_client.get(reverse(name, args=args))

        assert response.status_code == 200
        assert UMAMI_TAG not in response.content.decode()


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
    Run.objects.create(
        session=session,
        status=status,
        trigger_type=origin,
        repo_id="daiv/test",
        user=user,
        created_at=_local_noon(days_ago),
    )
    return session


def _gave_up(session, *, days_ago=0):
    """CI on the session's merge request ran out of attempts ``days_ago``."""
    make_event(
        session, EventKind.CI_FAILED, wake=False, reason=QuietReason.BUDGET_SPENT, merge_request_iid=9,
        created_at=_local_noon(days_ago),
    )  # fmt: skip


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
    def test_lists_every_source_questions_first(self, member_user, member_client):
        _classified_session(member_user, EnvelopeStatus.NEEDS_ATTENTION)
        _classified_session(member_user, EnvelopeStatus.FOUND_ISSUES)
        _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.WAITING_INPUT)
        _gave_up(_make_session(member_user, SessionOrigin.MR_WEBHOOK, days_ago=2), days_ago=2)
        _failing_schedule(member_user)
        _make_session(member_user, SessionOrigin.UI_JOB, status=RunStatus.FAILED)
        _make_session(member_user, SessionOrigin.CHAT)

        inbox = _inbox(member_client)

        assert _keys(inbox) == [
            "needs-input",
            "failed",
            "cant-run",
            "ci-still-failing",
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
        _gave_up(_make_session(member_user, SessionOrigin.MR_WEBHOOK, days_ago=8), days_ago=8)

        assert _keys(_inbox(member_client)) == ["found-issues"]

    def test_a_failure_leaves_fourteen_days_after_it_finished(self, member_user, member_client):
        for days_ago in (13, 15):
            session = _bare_session(member_user, SessionOrigin.UI_JOB)
            _add_latest_run(session, RunStatus.FAILED, days_ago=days_ago, prompt=f"Task {days_ago}")

        assert [item.at for item in _inbox(member_client)["items"]] == [_local_noon(13)]

    def test_an_old_question_stays(self, member_user, member_client):
        _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.WAITING_INPUT, days_ago=60)

        assert _keys(_inbox(member_client)) == ["needs-input"]

    def test_a_question_outranks_older_and_newer_failures(self, member_user, member_client):
        _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.FAILED, days_ago=0)
        _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.WAITING_INPUT, days_ago=1)
        _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.FAILED, days_ago=2)

        assert _keys(_inbox(member_client)) == ["needs-input", "failed", "failed"]

    def test_a_question_in_a_session_whose_ci_watch_gave_up_shows_as_the_question(self, member_user, member_client):
        _gave_up(_make_session(member_user, SessionOrigin.MR_WEBHOOK, status=RunStatus.WAITING_INPUT))

        assert _keys(_inbox(member_client)) == ["needs-input"]

    def test_ci_that_passed_after_giving_up_is_cleared(self, member_user, member_client):
        session = _make_session(member_user, SessionOrigin.MR_WEBHOOK, days_ago=2)
        _gave_up(session, days_ago=2)
        make_event(session, EventKind.CI_PASSED, merge_request_iid=9, created_at=timezone.now() - timedelta(days=1))

        assert _inbox(member_client)["items"] == []

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

    def test_leaves_out_archived_sessions(self, member_user, member_client):
        asking = _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.WAITING_INPUT)
        _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.FAILED)
        Session.objects.filter(pk=asking.pk).update(archived_at=timezone.now())

        assert _keys(_inbox(member_client)) == ["failed"]

    def test_caps_the_list_and_links_to_the_lists_it_came_from(self, member_user, member_client):
        for days_ago in range(INBOX_LIMIT + 2):
            _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.FAILED, days_ago=days_ago)
        _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.WAITING_INPUT)

        inbox = _inbox(member_client)

        assert len(inbox["items"]) == INBOX_LIMIT
        assert inbox["total"] == INBOX_LIMIT + 3
        assert _keys(inbox) == ["needs-input"] + ["failed"] * (INBOX_LIMIT - 1)
        failures = [item.at for item in inbox["items"][1:]]
        assert failures == sorted(failures, reverse=True)
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

    def test_a_fold_that_starts_open_after_your_week(self, member_user, member_client):
        _make_session(member_user, SessionOrigin.CHAT)

        main = BeautifulSoup(member_client.get(reverse("dashboard")).content, "html.parser").select_one("main")
        sections = main.select('[data-testid="your-week"], [data-testid="ways-to-use"]')

        assert [node["data-testid"] for node in sections] == ["your-week", "ways-to-use"]
        assert sections[1].name == "details"
        assert sections[1].has_attr("open")
        heading = main.select_one("#ways-heading")
        assert heading.name == "h2"
        assert not heading.find_parent("summary")

    def test_someone_new_gets_the_intro_and_the_slash_commands_open(self, member_user, member_client):
        def ways():
            return BeautifulSoup(member_client.get(reverse("dashboard")).content, "html.parser").select_one(
                '[data-testid="ways-to-use"]'
            )

        intro = "Start work from wherever you already are"
        first = ways()
        assert intro in first.get_text()
        assert first.select_one('[data-testid="ways-git"] details').has_attr("open")
        _make_session(member_user, SessionOrigin.CHAT)
        later = ways()
        assert intro not in later.get_text()
        assert not later.select_one('[data-testid="ways-git"] details').has_attr("open")


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
        assert context["activity"]["total_change"] == {"direction": "up", "amount": "100%", "was": "1", "tone": "good"}
        chat = context["breakdown"]["segments"][0]
        assert chat["value"] == 2
        assert (chat["change"]["was"], chat["change"]["relative"], chat["change"]["tone"]) == ("1", "100%", "good")

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

    def test_archived_sessions_are_still_counted(self, member_user, member_client):
        _make_session(member_user, SessionOrigin.CHAT)
        archived = _make_session(member_user, SessionOrigin.CHAT, status=RunStatus.FAILED)
        Session.objects.filter(pk=archived.pk).update(archived_at=timezone.now())

        breakdown = _insights(member_client)["breakdown"]

        assert (breakdown["sessions_total"], breakdown["sessions_failed"]) == (2, 1)
        assert _list_count(member_client, breakdown["sessions_url"]) == 2
        assert _list_count(member_client, breakdown["sessions_failed_url"]) == 1

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

        assert change == {"direction": "down", "amount": "100 pts", "was": "100%", "tone": "bad"}

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

    def test_still_counts_archived_sessions(self, member_user, member_client):
        _week_run(_bare_session(member_user))
        archived = _bare_session(member_user, title="Archived work")
        _week_run(archived, RunStatus.FAILED, merge_request_iid=7)
        Session.objects.filter(pk=archived.pk).update(archived_at=timezone.now())

        week = _week(member_client)

        assert (week["runs"], week["sessions"]) == (2, 2)
        assert len(week["merge_requests"]) == 1
        assert "archived=all" in week["chart"]["columns"][-1]["url"]

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

    @pytest.mark.parametrize("state", [MergeRequestState.MERGED, MergeRequestState.CLOSED])
    def test_a_merge_request_carries_its_state(self, member_user, member_client, state):
        session = _bare_session(member_user)
        _week_run(session, merge_request_iid=7)
        MergeRequestOwnership.objects.create(
            session=session,
            repo_id=session.repo_id,
            merge_request_iid=7,
            source_branch="chore/x",
            merged=state == MergeRequestState.MERGED,
            closed_at=timezone.now(),
        )

        assert _week(member_client)["merge_requests"][0]["state"] == state

    def test_a_merge_request_daiv_does_not_own_is_open(self, member_user, member_client):
        _week_run(_bare_session(member_user), merge_request_iid=7)

        assert _week(member_client)["merge_requests"][0]["state"] == MergeRequestState.OPEN

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

    def test_shown_when_all_your_sessions_are_archived(self, member_user, member_client):
        archived = _make_session(member_user, SessionOrigin.CHAT)
        Session.objects.filter(pk=archived.pk).update(archived_at=timezone.now())

        assert member_client.get(reverse("dashboard")).context["your_week"] is not None


@pytest.mark.django_db
class TestSidebarListsView:
    def test_requires_login(self, client):
        response = client.get(reverse("sidebar_lists"))

        assert response.status_code == 302
        assert response.url.startswith("/accounts/login/")

    @pytest.mark.parametrize(("query", "limit"), [("", 8), ("?limit=16", 16), ("?limit=9999", 9999), ("?limit=abc", 8)])
    def test_parses_limit(self, member_user, member_client, mocker, query, limit):
        spy = mocker.patch("dashboard.views.Sidebar")
        spy.return_value.lists.return_value = SidebarLists()

        member_client.get(reverse("sidebar_lists") + query)

        spy.assert_called_once_with(member_user, viewing="")
        spy.return_value.lists.assert_called_once_with(limit=limit)

    def test_passes_viewing(self, member_user, member_client, mocker):
        spy = mocker.patch("dashboard.views.Sidebar")
        spy.return_value.lists.return_value = SidebarLists()

        member_client.get(reverse("sidebar_lists") + "?viewing=thread-1")

        spy.assert_called_once_with(member_user, viewing="thread-1")

    @pytest.mark.parametrize(("query", "section"), [("?section=schedules", "schedules"), ("", "")])
    def test_highlights_the_section_of_the_page_it_refreshes(self, member_client, query, section):
        response = member_client.get(reverse("sidebar_lists") + query)

        assert response.context["nav_active_section"] == section
        heading = BeautifulSoup(response.content, "html.parser").select_one('[data-testid="nav-scheduled-group"] h2')
        assert ("sidebar__nav-item--active" in heading["class"]) is bool(section)


def _spend_page(client, **params):
    return client.get(reverse("insights_spend"), params)


@pytest.fixture
def other_user(django_user_model):
    return django_user_model.objects.create_user(username="other", email="other@x.io")


@pytest.mark.django_db
class TestInsightsSpendPage:
    def test_signed_out_visitors_are_sent_to_sign_in(self, client):
        assert _spend_page(client).status_code == 302

    @pytest.mark.parametrize(("client_fixture", "scope"), [("admin_client", "all"), ("member_client", "mine")])
    def test_opens_on_thirty_days_and_on_everyone_for_admins_and_mine_for_members(self, request, client_fixture, scope):
        response = _spend_page(request.getfixturevalue(client_fixture))

        assert response.status_code == 200
        assert (response.context["current_period"], response.context["current_scope"]) == ("30d", scope)
        assert response.context["nav_active_section"] == "insights"

    def test_an_invalid_period_or_scope_falls_back_to_the_default(self, member_client):
        context = _spend_page(member_client, period="1y", scope="theirs").context

        assert (context["current_period"], context["current_scope"]) == ("30d", "mine")

    def test_an_empty_period_says_so_alone(self, admin_client):
        soup = BeautifulSoup(_spend_page(admin_client).content, "html.parser")

        assert soup.select_one("[data-testid=spend-empty]").get_text(strip=True) == "Nothing was spent in this period"
        assert soup.select_one("[data-testid=spend-summary]") is None

    def test_both_insights_views_link_each_other_on_the_same_period(self, admin_client):
        for name in ("insights", "insights_spend"):
            soup = BeautifulSoup(admin_client.get(reverse(name), {"period": "90d"}).content, "html.parser")
            nav = soup.select_one("nav[aria-label=Insights]")
            hrefs = [f"{reverse('insights')}?period=90d", f"{reverse('insights_spend')}?period=90d"]
            assert [a["href"] for a in nav.select("a")] == hrefs
            assert nav.select_one("[aria-current]")["href"] == f"{reverse(name)}?period=90d"

    def test_tiles_badges_panels_and_tables_render(self, admin_user, admin_client):
        for index in range(7):
            run = spend_run(spend_session(admin_user, repo_id=f"r/{index}"), days_ago=index * 4)
            spend_record(run, model=f"m{index}", cost=str(index + 1), days_ago=index * 4, cache_reported=True)
        old = spend_run(spend_session(admin_user), days_ago=40)
        spend_record(old, days_ago=40, cost="1", cache_reported=True, cache_read_tokens=90)

        soup = BeautifulSoup(_spend_page(admin_client).content, "html.parser")

        tiles = [tile["data-testid"] for tile in soup.select(".stat-tile[data-testid^=tile-]")]
        assert tiles == ["tile-cost", "tile-tokens", "tile-cache", "tile-runs", "tile-avg", "tile-avg-changed"]
        tones = {
            tile["data-testid"]: tile.select_one("[data-testid=change-badge]")["data-tone"]
            for tile in soup.select(".stat-tile[data-testid^=tile-]")
            if tile.select_one("[data-testid=change-badge]")
        }
        assert (tones["tile-cost"], tones["tile-cache"], tones["tile-runs"]) == ("bad", "bad", "neutral")
        assert len(soup.select("[data-testid=spend-split]")) == 2
        assert soup.select("[data-testid=spend-chart] [data-testid=chart-card][role=tooltip]")
        repos = soup.select_one("[data-testid=breakdown-repository]")
        assert len(repos.select("[data-testid=hidden-row]")) == 2
        assert (
            repos.select_one("[data-testid=show-all]").get_text(" ", strip=True).startswith("Show all 7 repositories")
        )
        assert repos.select_one("[data-testid=rest-row]")
        started_by = soup.select_one("[data-testid=breakdown-started_by]")
        assert started_by.select_one("[data-testid=show-all]") is None
        assert soup.select_one("[data-testid=top-sessions]")
        assert soup.select_one("[data-testid=download-csv]")["href"].endswith("?period=30d&scope=all")

    def test_the_activity_chart_has_no_hover_cards(self, admin_user, admin_client):
        for days_ago in (0, 3):
            spend_run(spend_session(admin_user), days_ago=days_ago)

        soup = BeautifulSoup(admin_client.get(reverse("insights")).content, "html.parser")

        assert soup.select_one("[data-testid=runs-chart]")
        assert soup.select("[data-testid=chart-card]") == []

    def test_rows_without_a_list_say_why(self, admin_user, admin_client):
        spend_record(spend_run(spend_session(admin_user)))
        spend_record(None, task=SpendTask.MEMORY_CONSOLIDATION, repo_id="acme/api", cost="1")

        soup = BeautifulSoup(_spend_page(admin_client).content, "html.parser")
        people = soup.select_one("[data-testid=breakdown-person]")

        daiv = next(span for span in people.select("[data-testid=no-link]") if span.find(string=True).strip() == "DAIV")
        assert daiv["title"]
        assert daiv.select_one(".sr-only").get_text(strip=True) == f"({daiv['title']})"

    def test_the_mine_scope_says_what_it_covers(self, member_user, member_client):
        spend_record(spend_run(spend_session(member_user)))

        soup = BeautifulSoup(_spend_page(member_client).content, "html.parser")

        assert soup.select_one("[data-testid=mine-note]")
        assert "What your work with DAIV cost." in soup.get_text()

    def test_a_member_sees_nothing_from_a_repository_they_cannot_read(self, member_user, other_user, member_client):
        grant_read_access(member_user, "acme/api")
        spend_record(spend_run(spend_session(member_user)), model="mine-model")
        spend_record(spend_run(spend_session(other_user, repo_id="acme/api", title="Shared")), model="shared-model")
        secret = spend_run(spend_session(other_user, repo_id="acme/secret", title="Secret session"))
        spend_record(secret, model="secret-model", unpriced_tokens=5, cost="0", provider="secret-provider")

        mine = _spend_page(member_client, scope="mine")
        everyone = _spend_page(member_client, scope="all")

        assert mine.context["totals"]["cost"] == Decimal("1")
        assert everyone.context["totals"]["cost"] == Decimal("2")
        page = everyone.content.decode()
        for leak in ("acme/secret", "secret-model", "Secret session", "Secret-Provider"):
            assert leak not in page
        assert everyone.context["coverage"]["unpriced_models"] == []


def _streamed(response) -> str:
    """The body of a response streamed from an async iterator, as ASGI serves it."""

    async def _collect():
        return b"".join([chunk.encode() if isinstance(chunk, str) else chunk async for chunk in response])

    return async_to_sync(_collect)().decode()


@pytest.mark.django_db
class TestSpendExport:
    @pytest.fixture(autouse=True)
    def _fresh_throttle(self):
        cache.clear()

    @staticmethod
    def _csv(client, **params):
        response = client.get(reverse("insights_spend_export"), params)
        assert response.status_code == 200
        return response, list(csv.DictReader(io.StringIO(_streamed(response))))

    def test_past_ten_exports_an_hour_the_next_waits(self, admin_user, admin_client):
        statuses = [admin_client.get(reverse("insights_spend_export")).status_code for _ in range(11)]

        assert statuses == [200] * 10 + [429]

    def test_the_cost_column_sums_to_the_page_total(self, admin_user, admin_client):
        for index in range(3):
            spend_record(spend_run(spend_session(admin_user)), cost=f"{index}.25", days_ago=index)
        spend_record(None, task=SpendTask.TITLE, user=admin_user, cost="0.5")
        spend_record(spend_run(spend_session(admin_user), days_ago=45), cost="9", days_ago=45)

        response, rows = self._csv(admin_client, period="30d")

        total = _spend_page(admin_client, period="30d").context["totals"]["cost"]
        assert sum(Decimal(row["cost_usd"]) for row in rows) == total
        assert response["Content-Type"] == "text/csv; charset=utf-8"
        assert response["Content-Disposition"].startswith('attachment; filename="daiv-spend-')

    def test_the_header_follows_the_contract(self, admin_client):
        response = admin_client.get(reverse("insights_spend_export"))

        header = _streamed(response).splitlines()[0]

        assert header.split(",") == list(COLUMNS)

    def test_a_value_a_spreadsheet_would_run_as_a_formula_is_quoted(self, admin_client):
        spend_record(
            None, task=SpendTask.TITLE, deleted_user_id=7, person_name='=HYPERLINK("http://x")', repo_id="@acme/api"
        )

        _response, rows = self._csv(admin_client)

        assert (rows[0]["person"], rows[0]["repo_id"]) == ('\'=HYPERLINK("http://x")', "'@acme/api")
        assert rows[0]["cost_usd"] == "1.000000"

    def test_a_member_never_gets_a_row_from_a_private_repository(self, member_user, other_user, member_client):
        grant_read_access(member_user, "acme/api")
        spend_record(spend_run(spend_session(other_user, repo_id="acme/api")))
        spend_record(spend_run(spend_session(other_user, repo_id="acme/secret")), repo_id="acme/secret")

        _response, rows = self._csv(member_client, scope="all")

        assert [row["repo_id"] for row in rows] == ["acme/api"]
