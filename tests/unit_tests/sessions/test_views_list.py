from __future__ import annotations

import re
import uuid
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlsplit

from django.db import connection
from django.http import QueryDict
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import has_vary_header

import pytest
from bs4 import BeautifulSoup
from dashboard.insights import window_for
from dashboard.spend import SpendReport
from sessions.models import (
    MergeRequestOwnership,
    MergeRequestState,
    Run,
    RunStatus,
    Session,
    SessionOrigin,
    SpendRecord,
    SpendTask,
)

from accounts.models import User
from schedules.models import Frequency, ScheduledJob


@pytest.fixture
def user(db):
    return User.objects.create_user(
        username="alice",
        email="alice@test.com",
        password="testpass123",  # noqa: S106
    )


@pytest.fixture
def logged_in_client(user):
    client = Client()
    client.force_login(user)
    return client


def _create_session(**kwargs) -> Session:
    defaults = {
        "thread_id": str(uuid.uuid4()),
        "origin": SessionOrigin.SCHEDULE,
        "repo_id": "group/project",
        "ref": "main",
    }
    defaults.update(kwargs)
    return Session.objects.create(**defaults)


def _create_schedule(user: User, name: str, repo_id: str = "group/project") -> ScheduledJob:
    return ScheduledJob.objects.create(
        user=user,
        name=name,
        prompt="p",
        repos=[{"repo_id": repo_id, "ref": ""}],
        frequency=Frequency.DAILY,
        time="09:00",
    )


def _create_run(session: Session, **kwargs) -> Run:
    defaults = {
        "session": session,
        "trigger_type": SessionOrigin.SCHEDULE,
        "repo_id": session.repo_id,
        "status": RunStatus.SUCCESSFUL,
    }
    defaults.update(kwargs)
    return Run.objects.create(**defaults)


def _own(session: Session, iid: int, state: str = MergeRequestState.OPEN, *, repo_id: str = "") -> None:
    MergeRequestOwnership.objects.create(
        session=session,
        repo_id=repo_id or session.repo_id,
        merge_request_iid=iid,
        source_branch=f"branch-{iid}",
        merged=state == MergeRequestState.MERGED,
        closed_at=None if state == MergeRequestState.OPEN else timezone.now(),
    )


def _row(soup: BeautifulSoup, session: Session):
    """The list row of ``session``, found by its stretched link to the session page."""
    href = reverse("session_detail", kwargs={"thread_id": session.thread_id})
    return soup.select_one(f'#session-results a[href="{href}"]').find_parent(class_="session-row")


def _shown_chips(soup: BeautifulSoup) -> list[str]:
    """The keys of the filter chips a first paint shows, in order."""
    return [chip["data-chip"] for chip in soup.select("[data-chip]") if "display:none" not in chip.get("style", "")]


def _chip_value(soup: BeautifulSoup, key: str) -> str:
    return soup.select_one(f"[data-chip={key}] .filter-chip__value").get_text(" ", strip=True)


@pytest.mark.django_db
class TestSessionListView:
    def test_unauthenticated_redirects_to_login(self):
        response = Client().get(reverse("session_list"))
        assert response.status_code == 302
        assert "/accounts/login/" in response.url

    def test_authenticated_user_can_access(self, logged_in_client, user):
        response = logged_in_client.get(reverse("session_list"))
        assert response.status_code == 200

    def test_varies_on_the_htmx_header(self, logged_in_client, user):
        assert has_vary_header(logged_in_client.get(reverse("session_list")), "HX-Request")

    def test_has_no_breadcrumb(self, logged_in_client):
        response = logged_in_client.get(reverse("session_list"))
        assert b'data-testid="app-breadcrumb"' not in response.content

    def test_owner_scoping_excludes_other_users_sessions(self, logged_in_client, user):
        """Owner scoping is applied before filters — another user's sessions are invisible."""
        mine = _create_session(user=user, repo_id="mine/repo")
        other_user = User.objects.create_user(
            username="bob",
            email="bob@test.com",
            password="testpass123",  # noqa: S106
        )
        theirs = _create_session(user=other_user, repo_id="mine/repo")

        response = logged_in_client.get(reverse("session_list"), {"repo": "mine/repo"})

        assert response.status_code == 200
        sessions = list(response.context["sessions"])
        session_pks = [s.pk for s in sessions]
        assert mine.pk in session_pks
        assert theirs.pk not in session_pks

    def test_a_plain_visit_shows_only_the_users_own_sessions(self, admin_client, admin_user):
        client = admin_client
        mine = _create_session(user=admin_user)
        theirs = _create_session(user=User.objects.create_user(username="bob", email="b@test.com", password="x"))  # noqa: S106

        response = client.get(reverse("session_list"))

        assert [s.pk for s in response.context["sessions"]] == [mine.pk]
        assert response.context["current_scope"] == "mine"
        assert response.context["has_active_filters"] is False
        response = client.get(reverse("session_list"), {"scope": "all"})
        assert {s.pk for s in response.context["sessions"]} == {mine.pk, theirs.pk}

    def test_a_deep_link_defaults_to_all_and_clear_all_keeps_it(self, admin_client):
        client = admin_client
        theirs = _create_session(user=User.objects.create_user(username="bob", email="b@test.com", password="x"))  # noqa: S106

        response = client.get(reverse("session_list"), {"repo": "group/project"})

        assert [s.pk for s in response.context["sessions"]] == [theirs.pk]
        assert response.context["current_scope"] == "all"
        assert f'href="{reverse("session_list")}?scope=all"' in response.content.decode()

    @pytest.mark.parametrize("param", ["repo", "schedule", "batch", "mr"])
    def test_a_deep_link_without_scope_shows_teammates_sessions(self, admin_client, admin_user, other_user, param):
        schedule = _create_schedule(other_user, "Nightly")
        batch_id = uuid.uuid4()
        _create_session(user=admin_user, repo_id="group/other")
        theirs = _create_session(user=other_user, scheduled_job=schedule, merge_request_iid=42)
        _create_run(theirs, batch_id=batch_id)
        value = {"repo": "group/project", "schedule": str(schedule.pk), "batch": str(batch_id), "mr": "42"}[param]

        response = admin_client.get(reverse("session_list"), {param: value})

        assert [s.pk for s in response.context["sessions"]] == [theirs.pk]
        assert response.context["current_scope"] == "all"
        response = admin_client.get(reverse("session_list"), {param: value, "scope": "mine"})
        assert list(response.context["sessions"]) == []

    def test_filter_bar_renders_the_scope_toggle(self, logged_in_client, user):
        soup = BeautifulSoup(logged_in_client.get(reverse("session_list")).content, "html.parser")

        items = soup.select("nav.segmented-nav .segmented-nav__item")
        assert [item["@click.prevent"] for item in items] == ["setScope('mine')", "setScope('all')"]

    def test_search_and_date_inputs_have_aria_labels(self, logged_in_client, user):
        html = logged_in_client.get(reverse("session_list")).content.decode()
        assert 'aria-label="Search by title or repository"' in html
        assert 'aria-label="From date"' in html
        assert 'aria-label="To date"' in html

    def test_archived_sessions_have_a_list_of_their_own(self, logged_in_client, user):
        shown = _create_session(user=user)
        hidden = _create_session(user=user, archived_at=timezone.now())

        default = logged_in_client.get(reverse("session_list"))
        archived = logged_in_client.get(reverse("session_list"), {"archived": "true"})
        unrecognised = logged_in_client.get(reverse("session_list"), {"archived": "1"})

        assert [s.pk for s in default.context["sessions"]] == [shown.pk]
        assert [s.pk for s in archived.context["sessions"]] == [hidden.pk]
        assert [s.pk for s in unrecognised.context["sessions"]] == [shown.pk]
        assert (default.context["current_archived"], archived.context["current_archived"]) == (False, True)
        assert unrecognised.context["current_archived"] is False
        assert archived.context["has_active_filters"] is True

    def test_archived_all_lists_archived_sessions_with_the_rest(self, logged_in_client, user):
        shown = _create_session(user=user)
        archived = _create_session(user=user, archived_at=timezone.now())

        response = logged_in_client.get(reverse("session_list"), {"archived": "all"})

        assert {s.pk for s in response.context["sessions"]} == {shown.pk, archived.pk}
        assert (response.context["current_archived"], response.context["current_archived_included"]) == (False, True)
        assert response.context["has_active_filters"] is True
        assert "archived_matches" not in response.context
        assert 'data-testid="filter-archived-included"' in response.content.decode()

    def test_a_filtered_list_says_how_many_archived_sessions_match_too(self, logged_in_client, user):
        _create_session(user=user, repo_id="group/project", merge_request_iid=7)
        for _ in range(2):
            _create_session(user=user, repo_id="group/project", merge_request_iid=7, archived_at=timezone.now())
        _create_session(user=user, repo_id="group/other", archived_at=timezone.now())

        resp = logged_in_client.get(reverse("session_list"), {"repo": "group/project", "mr": "7"})
        html = resp.content.decode()

        assert resp.context["archived_matches"] == 2
        assert "2 archived sessions match too." in html
        assert "?repo=group%2Fproject&amp;mr=7&amp;archived=true" in html

    def test_an_unfiltered_or_archived_list_does_not_count_archived_matches(self, logged_in_client, user):
        _create_session(user=user, archived_at=timezone.now())

        unfiltered = logged_in_client.get(reverse("session_list"))
        archived = logged_in_client.get(reverse("session_list"), {"archived": "true"})

        assert "archived_matches" not in unfiltered.context
        assert "archived_matches" not in archived.context

    def test_filter_bar_renders_the_archived_toggle(self, logged_in_client, user):
        html = logged_in_client.get(reverse("session_list"), {"archived": "true"}).content.decode()
        control = html[html.index('data-testid="filter-archived"') - 400 : html.index('data-testid="filter-archived"')]

        assert 'aria-pressed="true"' in control
        assert "toggleArchived()" in control

    def test_hides_ci_watch_placeholder_until_a_run_lands(self, logged_in_client, user):
        placeholder = _create_session(user=user, origin=SessionOrigin.PIPELINE_WEBHOOK, merge_request_iid=7)
        fixed = _create_session(user=user, origin=SessionOrigin.PIPELINE_WEBHOOK, merge_request_iid=8)
        _create_run(fixed, trigger_type=SessionOrigin.PIPELINE_WEBHOOK)
        legacy_chat = _create_session(user=user, origin=SessionOrigin.CHAT)

        response = logged_in_client.get(reverse("session_list"))

        session_pks = {s.pk for s in response.context["sessions"]}
        assert placeholder.pk not in session_pks
        assert {fixed.pk, legacy_chat.pk} <= session_pks

    def test_filter_by_status_on_latest_run(self, logged_in_client, user):
        """?status=SUCCESSFUL only returns sessions whose latest run is SUCCESSFUL."""
        success_session = _create_session(user=user)
        _create_run(success_session, status=RunStatus.SUCCESSFUL)

        failed_session = _create_session(user=user)
        _create_run(failed_session, status=RunStatus.FAILED)

        response = logged_in_client.get(reverse("session_list"), {"status": RunStatus.SUCCESSFUL})

        assert response.status_code == 200
        sessions = list(response.context["sessions"])
        session_pks = [s.pk for s in sessions]
        assert success_session.pk in session_pks
        assert failed_session.pk not in session_pks

    def test_invalid_filter_drops_silently(self, logged_in_client, user):
        """?status=bogus shows full list (strict=False) and current_status is empty."""
        session = _create_session(user=user)

        response = logged_in_client.get(reverse("session_list"), {"status": "bogus"})

        assert response.status_code == 200
        session_pks = [s.pk for s in response.context["sessions"]]
        assert session.pk in session_pks
        assert response.context["current_status"] == ""

    def test_context_includes_origins_and_statuses(self, logged_in_client, user):
        """Context must include origins and statuses choice lists."""
        response = logged_in_client.get(reverse("session_list"))
        assert response.status_code == 200
        assert "origins" in response.context
        assert "statuses" in response.context
        assert len(response.context["origins"]) > 0
        assert len(response.context["statuses"]) > 0

    def test_status_filter_renders_needs_input_and_failed_first(self, logged_in_client, user):
        response = logged_in_client.get(reverse("session_list"))
        html = response.content.decode()
        positions = [
            html.index(f'data-filter-key="status" data-value="{status}"')
            for status in ("WAITING_INPUT", "FAILED", "SUCCESSFUL")
        ]
        assert positions == sorted(positions)

    def test_date_param_names_are_date_from_and_date_to(self, logged_in_client, user):
        """Lock in the URL param names; values round-trip to template context."""
        _create_session(user=user)
        response = logged_in_client.get(reverse("session_list"), {"date_from": "2020-01-01", "date_to": "2100-01-01"})
        assert response.status_code == 200
        assert response.context["current_from"] == "2020-01-01"
        assert response.context["current_to"] == "2100-01-01"

    def test_has_active_filters_false_with_no_params(self, logged_in_client, user):
        _create_session(user=user)
        response = logged_in_client.get(reverse("session_list"))
        assert response.context["has_active_filters"] is False
        assert response.context["current_batch_short"] == ""

    def test_has_active_filters_true_when_batch_is_set(self, logged_in_client, user):
        batch_id = uuid.uuid4()
        _create_session(user=user)
        response = logged_in_client.get(reverse("session_list"), {"batch": str(batch_id)})
        assert response.context["has_active_filters"] is True
        assert response.context["current_batch_short"] == str(batch_id)[:8]

    def test_in_flight_ids_contains_non_terminal_run_ids(self, logged_in_client, user):
        """in_flight_ids is a comma-joined string of non-terminal run PKs from page sessions."""
        session = _create_session(user=user)
        running_run = _create_run(session, status=RunStatus.RUNNING)
        _create_run(session, status=RunStatus.SUCCESSFUL)  # terminal — excluded

        response = logged_in_client.get(reverse("session_list"))
        assert response.status_code == 200

        in_flight_ids = response.context["in_flight_ids"]
        # The running run should be in the comma-joined string.
        assert str(running_run.pk) in in_flight_ids

    def test_in_flight_ids_excludes_terminal_runs(self, logged_in_client, user):
        """Terminal runs (SUCCESSFUL, FAILED) must not appear in in_flight_ids."""
        session = _create_session(user=user)
        successful_run = _create_run(session, status=RunStatus.SUCCESSFUL)
        failed_run = _create_run(session, status=RunStatus.FAILED)

        response = logged_in_client.get(reverse("session_list"))
        in_flight_ids = response.context["in_flight_ids"]

        assert str(successful_run.pk) not in in_flight_ids
        assert str(failed_run.pk) not in in_flight_ids

    def test_session_row_links_to_detail(self, logged_in_client, user):
        """Each row renders a stretched anchor to session_detail so the whole row is clickable."""
        session = _create_session(user=user)

        response = logged_in_client.get(reverse("session_list"))

        assert response.status_code == 200
        expected_href = reverse("session_detail", kwargs={"thread_id": session.thread_id})
        assert f'href="{expected_href}"' in response.content.decode()

    def test_pagination_uses_paginate_by(self, logged_in_client, user):
        """Check that paginated results are returned when there are more than paginate_by sessions."""
        # Create 30 sessions to exceed typical paginate_by=25.
        for _ in range(30):
            _create_session(user=user)

        response = logged_in_client.get(reverse("session_list"))
        assert response.status_code == 200
        # Page 1 should have at most 25 sessions (default paginate_by).
        assert len(response.context["sessions"]) <= 25

        # Page 2 should exist.
        response_p2 = logged_in_client.get(reverse("session_list"), {"page": "2"})
        assert response_p2.status_code == 200
        assert len(response_p2.context["sessions"]) > 0

    def test_context_has_search_and_range(self, logged_in_client, user):
        _create_session(user=user)
        response = logged_in_client.get(reverse("session_list"), {"q": "hello", "range": "7d"})
        assert response.status_code == 200
        assert response.context["current_q"] == "hello"
        assert response.context["current_range"] == "7d"
        assert _chip_value(BeautifulSoup(response.content, "html.parser"), "time") == "Last 7 days"
        assert response.context["has_active_filters"] is True

    def test_has_active_filters_true_when_q_set(self, logged_in_client, user):
        _create_session(user=user)
        response = logged_in_client.get(reverse("session_list"), {"q": "abc"})
        assert response.context["has_active_filters"] is True

    def test_mr_filter_is_visible_and_clearable(self, logged_in_client, user):
        """Arriving from a merge request description, the filter needs a chip — without one the
        list looks arbitrarily short and the whole chip row (with "Clear all") stays hidden."""
        _create_session(user=user, merge_request_iid=42)

        response = logged_in_client.get(reverse("session_list"), {"mr": "42"})

        assert response.context["current_mr"] == 42
        assert response.context["has_active_filters"] is True
        soup = BeautifulSoup(response.content, "html.parser")
        assert _shown_chips(soup) == ["mr"]
        assert _chip_value(soup, "mr") == "!42"

    def test_schedule_filter_chip_names_the_schedule(self, logged_in_client, user):
        schedule = _create_schedule(user, "Nightly audit")
        _create_session(user=user, scheduled_job=schedule)

        response = logged_in_client.get(reverse("session_list"), {"schedule": str(schedule.pk)})

        assert response.context["schedule_name"] == "Nightly audit"
        assert _chip_value(BeautifulSoup(response.content, "html.parser"), "schedule") == "Nightly audit"

    def test_schedule_filter_chip_hides_another_users_schedule_name(self, logged_in_client, other_user):
        schedule = _create_schedule(other_user, "Secret audit", repo_id="other/secret")
        _create_session(user=other_user, repo_id="other/secret", scheduled_job=schedule)

        response = logged_in_client.get(reverse("session_list"), {"schedule": str(schedule.pk)})

        assert response.context["schedule_name"] == ""
        assert "Secret audit" not in response.content.decode()

    def test_schedule_filter_chip_names_the_schedule_when_mine_has_no_rows(self, admin_client, other_user):
        schedule = _create_schedule(other_user, "Their audit")
        _create_session(user=other_user, scheduled_job=schedule)

        response = admin_client.get(reverse("session_list"), {"schedule": str(schedule.pk), "scope": "mine"})

        assert list(response.context["sessions"]) == []
        assert response.context["schedule_name"] == "Their audit"

    def test_clearing_a_deep_link_chip_keeps_the_scope(self, admin_client):
        _create_session(repo_id="group/project", merge_request_iid=42)

        html = admin_client.get(reverse("session_list"), {"repo": "group/project", "mr": "42"}).content.decode()

        assert 'href="?scope=all"' in html
        assert 'href="?repo=group%2Fproject&amp;scope=all"' in html

    def test_runs_are_prefetched_newest_first(self, logged_in_client, user, django_assert_num_queries):
        session = _create_session(user=user)
        _create_run(session, status=RunStatus.SUCCESSFUL)
        newest = _create_run(session, status=RunStatus.RUNNING)
        response = logged_in_client.get(reverse("session_list"))
        row = next(s for s in response.context["sessions"] if s.pk == session.pk)
        with django_assert_num_queries(0):
            latest = list(row.runs.all())[0]  # served from the prefetch cache — no query
        assert latest.pk == newest.pk

    def test_row_links_the_merge_request_a_job_run_published(self, logged_in_client, user):
        session = _create_session(user=user, origin=SessionOrigin.API_JOB)
        _create_run(session, merge_request_iid=31, merge_request_web_url="https://git.example/mr/31")

        html = logged_in_client.get(reverse("session_list")).content.decode()

        assert re.search(r'<a href="https://git\.example/mr/31"[^>]*class="mr-pill[^"]*">.*?!31</a>', html, re.DOTALL)

    @pytest.mark.parametrize("state", MergeRequestState)
    def test_row_pill_shows_the_merge_request_state(self, logged_in_client, user, state):
        session = _create_session(user=user, origin=SessionOrigin.API_JOB)
        _create_run(session, merge_request_iid=31, merge_request_web_url="https://git.example/mr/31")
        _own(session, 31, state)

        html = logged_in_client.get(reverse("session_list")).content.decode()

        assert re.search(rf'title="{state.label}"\s+class="mr-pill mr-pill--{state} [^"]*">.*?!31</a>', html, re.DOTALL)

    def test_row_pill_shows_the_state_when_another_session_owns_it(self, logged_in_client, user):
        session = _create_session(user=user, origin=SessionOrigin.API_JOB)
        _create_run(session, merge_request_iid=31, merge_request_web_url="https://git.example/mr/31")
        _own(_create_session(user=user), 31, MergeRequestState.MERGED)

        html = logged_in_client.get(reverse("session_list")).content.decode()

        assert html.count("mr-pill--merged") == 1

    def test_row_pill_ignores_a_same_numbered_merge_request_in_another_repository(self, logged_in_client, user):
        session = _create_session(user=user, origin=SessionOrigin.API_JOB)
        _create_run(session, merge_request_iid=31, merge_request_web_url="https://git.example/mr/31")
        _own(session, 31, MergeRequestState.MERGED, repo_id="group/other")

        html = logged_in_client.get(reverse("session_list")).content.decode()

        assert "mr-pill--merged" not in html

    def test_row_shows_the_model_the_latest_run_ran_on(self, logged_in_client, user):
        session = _create_session(user=user, origin=SessionOrigin.API_JOB)
        _create_run(session, agent_model="openrouter:z-ai/glm-5.2")

        html = logged_in_client.get(reverse("session_list")).content.decode()

        assert re.search(r'<span class="meta-pill[^"]*">openrouter:z-ai/glm-5\.2</span>', html)

    def test_more_rows_cost_no_more_queries(self, logged_in_client, user, django_assert_num_queries):
        """A run field the row reads but ``.only()`` leaves out costs a query per row."""

        def publish(iid):
            session = _create_session(user=user)
            _create_run(session, merge_request_iid=iid, merge_request_web_url=f"https://git/{iid}")
            _own(session, iid, MergeRequestState.MERGED)

        publish(1)
        logged_in_client.get(reverse("session_list"))  # the first render also warms per-process caches
        with CaptureQueriesContext(connection) as baseline:
            logged_in_client.get(reverse("session_list"))
        for iid in (2, 3, 4):
            publish(iid)

        with django_assert_num_queries(len(baseline.captured_queries)):
            logged_in_client.get(reverse("session_list"))

    def test_header_has_new_cta(self, logged_in_client, user):
        response = logged_in_client.get(reverse("session_list"))
        html = response.content.decode()
        # Anchor to the page's primary CTA button, not a bare `"New" in html` (which
        # appears in unrelated chrome and can never fail) nor a bare URL check (the
        # sidebar also links to session_new, so that passes even if the header CTA
        # regresses). The trailing quote in the href pins it to session_new, not the
        # session_new_chat route whose path is a superstring.
        target = f'href="{reverse("session_new")}"'
        ctas = [a for a in re.findall(r"<a\b[^>]*>.*?</a>", html, re.DOTALL) if target in a and "btn-primary" in a]
        assert ctas, "no btn-primary CTA pointing at session_new"
        assert any("New" in a for a in ctas)

    def test_filter_bar_has_search_input(self, logged_in_client, user):
        response = logged_in_client.get(reverse("session_list"))
        assert 'x-model="q"' in response.content.decode()

    def test_row_dot_shows_only_while_running_or_needing_input(self, logged_in_client, user):
        sessions = {}
        for status in (
            RunStatus.RUNNING,
            RunStatus.WAITING_INPUT,
            RunStatus.SUCCESSFUL,
            RunStatus.FAILED,
            RunStatus.QUEUED,
        ):
            sessions[status] = _create_session(user=user)
            _create_run(sessions[status], status=status)
        sessions[None] = _create_session(user=user)

        soup = BeautifulSoup(logged_in_client.get(reverse("session_list")).content, "html.parser")

        quiet = {
            status
            for status, session in sessions.items()
            if "invisible" in _row(soup, session).select_one(".session-dot-slot .status-dot")["class"]
        }
        assert quiet == {RunStatus.SUCCESSFUL, RunStatus.FAILED, RunStatus.QUEUED, None}

    def test_a_failed_row_keeps_its_edge_and_carries_its_run(self, logged_in_client, user):
        failed = _create_session(user=user)
        run = _create_run(failed, status=RunStatus.FAILED)
        idle = _create_session(user=user)

        soup = BeautifulSoup(logged_in_client.get(reverse("session_list")).content, "html.parser")

        row = _row(soup, failed)
        assert "session-row--failed" in row["class"]
        assert (row["data-run-id"], row["data-run-status"]) == (str(run.id), "FAILED")
        assert not _row(soup, idle).has_attr("data-run-id")
        assert not _row(soup, idle).has_attr("data-run-status")

    def test_row_puts_repo_and_branch_under_the_title(self, logged_in_client, user):
        session = _create_session(user=user, title="Do the thing", repo_id="group/project", ref="main")
        _create_run(session, merge_request_iid=7, merge_request_web_url="https://git.example/mr/7")
        _own(session, 7)
        bare = _create_session(user=user, title="Just chat", repo_id="", ref="")

        soup = BeautifulSoup(logged_in_client.get(reverse("session_list")).content, "html.parser")

        title = _row(soup, session).select_one(".session-row__title")
        assert "Do the thing" in title.get_text()
        assert title.select_one(".mr-pill") is not None
        meta = _row(soup, session).select_one(".session-row__meta")
        assert "group/project" in meta.get_text()
        assert "main" in meta.get_text()
        assert _row(soup, bare).select_one(".session-row__meta") is None

    def test_row_age_is_short_with_the_full_date_in_its_title(self, logged_in_client, user):
        session = _create_session(user=user)
        then = timezone.now() - timedelta(days=21)
        Session.objects.filter(pk=session.pk).update(last_active_at=then)

        soup = BeautifulSoup(logged_in_client.get(reverse("session_list")).content, "html.parser")

        age = _row(soup, session).select_one(".session-age")
        assert age.get_text(strip=True) == "3w"
        assert str(then.year) in age["title"]

    def test_row_shows_branch_and_day_group(self, logged_in_client, user):
        session = _create_session(user=user, ref="feat/x", title="Do the thing")
        _create_run(session, status=RunStatus.SUCCESSFUL)
        response = logged_in_client.get(reverse("session_list"))
        html = response.content.decode()
        assert "Do the thing" in html
        assert "feat/x" in html
        # Narrow to the day-group header markup: "Today" also appears in the Time filter
        # menu, so a bare substring check could pass even if row grouping regressed.
        assert "session-group-header" in html
        assert "<span>Today</span>" in html  # day-group header for a just-created session

    def test_row_shows_repository(self, logged_in_client, user):
        session = _create_session(user=user, repo_id="group/project", title="Do the thing")
        _create_run(session, status=RunStatus.SUCCESSFUL)
        response = logged_in_client.get(reverse("session_list"))
        html = response.content.decode()
        # Anchor to the row's repo element — repo_id can appear in other markup
        # (filter chips, data attrs), so a bare substring check could false-pass.
        assert "session-repo" in html
        assert "group/project" in html

    def test_date_param_is_js_escaped_in_x_data(self, logged_in_client, user):
        """XSS: date params must be JS-escaped in the Alpine x-data attribute.

        Django's HTML autoescape converts ' → &#x27;, which looks safe in HTML but is
        NOT safe in a JS-eval context: the browser HTML-decodes the attribute value
        before Alpine evaluates it as JavaScript, so &#x27; becomes ' and the injected
        payload runs.

        Without |escapejs the HTML-encoded payload (&#x27;});alert) appears inside the
        x-data= attribute value; with |escapejs it is rendered as \\u0027 instead.

        Note: &#x27;});alert may still appear in the button label (HTML text context, safe),
        so this test narrows its check to the x-data attribute value specifically.
        """
        payload = "'});alert(1);({'"
        response = logged_in_client.get(reverse("session_list"), {"date_from": payload})
        html = response.content.decode()
        # Without |escapejs, the x-data attribute contains the HTML-encoded form which is
        # still exploitable (browser decodes &#x27; → ' before Alpine evaluates the JS).
        # The raw injected form inside x-data= must not be present.
        assert "x-data=\"{ from: '&#x27;" not in html
        # The JS-safe unicode escape must be present inside the x-data attribute (confirms |escapejs fired).
        assert "x-data=\"{ from: '\\u0027" in html

    def test_htmx_request_renders_results_fragment_only(self, logged_in_client, user):
        """An HX-Request GET renders the results fragment, not the full page chrome."""
        _create_session(user=user)
        response = logged_in_client.get(reverse("session_list"), HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        template_names = [t.name for t in response.templates if t.name]
        assert "sessions/_session_results.html" in template_names
        assert "sessions/session_list.html" not in template_names
        # The <h1> page header lives only in the full page, never in the fragment.
        assert "Agent sessions" not in response.content.decode()

    def test_normal_request_renders_full_page(self, logged_in_client, user):
        """A normal GET renders the full page, which inlines the results fragment on first paint.

        The fragment (with paginate_swap on) must be present before any HTMX swap, so
        client-side pagination and the SSE re-arm work on first load. This also pins the
        DOM ids (``session-results``/``session-in-flight``) the JS silently depends on.
        """
        for _ in range(30):  # exceed paginate_by so pagination links render
            _create_session(user=user)
        response = logged_in_client.get(reverse("session_list"))
        template_names = [t.name for t in response.templates if t.name]
        assert "sessions/session_list.html" in template_names
        assert "sessions/_session_results.html" in template_names
        html = response.content.decode()
        assert 'id="session-results"' in html
        assert 'id="session-in-flight"' in html
        assert "data-page-swap" in html  # swap-marked from first paint, not only after a swap

    def test_htmx_fragment_carries_in_flight_ids(self, logged_in_client, user):
        """The fragment exposes in-flight run ids for the SSE re-arm to read after a swap."""
        session = _create_session(user=user)
        running = _create_run(session, status=RunStatus.RUNNING)
        response = logged_in_client.get(reverse("session_list"), HTTP_HX_REQUEST="true")
        html = response.content.decode()
        assert 'id="session-in-flight"' in html
        assert str(running.pk) in html

    def test_htmx_pagination_links_are_swap_marked(self, logged_in_client, user):
        """With >paginate_by sessions, the fragment's pagination <a> links carry data-page-swap."""
        for _ in range(30):
            _create_session(user=user)
        response = logged_in_client.get(reverse("session_list"), HTTP_HX_REQUEST="true")
        html = response.content.decode()
        # The marker must sit on a pagination anchor next to a page= href, not leak onto
        # some unrelated element (a bare substring check would miss that regression).
        assert re.search(r'href="[^"]*page=\d+[^"]*"[^>]*data-page-swap', html)

    def test_htmx_no_match_fragment_uses_filtered_empty_state(self, logged_in_client, user):
        """An HX-Request whose filters exclude everything renders the no-match state.

        The user has data, just none matching — so the fragment must render the
        ``has_active_filters`` branch, not fall through to the true-empty CTA. Guards
        that ``has_active_filters`` stays wired on the HTMX fragment path.
        """
        _create_session(user=user)  # exists, but the search term below won't match it
        response = logged_in_client.get(reverse("session_list"), {"q": "zznomatchzz"}, HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        html = response.content.decode()
        assert "No sessions match these filters." in html
        assert "Start your first run" not in html

    def test_htmx_empty_account_fragment_shows_first_run_cta(self, logged_in_client, user):
        """With no sessions and no filters, the HX-Request fragment shows the true-empty CTA."""
        response = logged_in_client.get(reverse("session_list"), HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        html = response.content.decode()
        assert "Start your first run" in html
        assert "No sessions match these filters." not in html

    def test_row_latest_run_matches_status_filter_on_equal_created_at(self, logged_in_client, user):
        """DISPLAY order must match the FILTER tiebreaker (-created_at, -id).

        When two runs share the same created_at timestamp (e.g. batch runs created in
        one transaction), managers.py picks the higher-id run for status FILTERING via
        order_by("-created_at", "-id").  The prefetch in views.py must use the same
        tiebreaker so the DISPLAY first-run (row.runs.all()[0]) is the SAME run whose
        status the annotation selected.
        """
        import datetime

        from django.utils import timezone

        session = _create_session(user=user)
        run_a = _create_run(session, status=RunStatus.FAILED)
        run_b = _create_run(session, status=RunStatus.SUCCESSFUL)

        # Determine which run has the higher UUID (the -id tiebreaker compares UUIDs
        # lexicographically, not by insertion order — UUIDv4 is random).
        run_winner = run_a if run_a.pk > run_b.pk else run_b

        # Force both runs to an identical created_at so the only tiebreaker is id.
        fixed_dt = timezone.make_aware(datetime.datetime(2024, 1, 1, 12, 0, 0))
        Run.objects.filter(pk__in=[run_a.pk, run_b.pk]).update(created_at=fixed_dt)

        response = logged_in_client.get(reverse("session_list"))
        assert response.status_code == 200

        row = next(s for s in response.context["sessions"] if s.pk == session.pk)
        # DISPLAY: first run from the prefetch must be the higher-id run (matching -id tiebreaker).
        display_first = list(row.runs.all())[0]
        assert display_first.pk == run_winner.pk, (
            f"Prefetch tiebreaker mismatch: display first run pk={display_first.pk} "
            f"but expected pk={run_winner.pk} (higher id, matching managers.py -id tiebreaker)"
        )


def test_pagination_partial_swap_marker_is_opt_in(rf):
    """The shared pagination partial emits data-page-swap only when paginate_swap is passed."""
    from django.template.loader import render_to_string

    class _Paginator:
        num_pages = 3

    class _Page:
        number = 1
        paginator = _Paginator()
        next_page_number = 2

        def has_previous(self):
            return False

        def has_next(self):
            return True

    def _render(**extra):
        return render_to_string(
            "core/_pagination.html", {"is_paginated": True, "page_obj": _Page(), **extra}, request=rf.get("/x/?foo=bar")
        )

    assert "data-page-swap" not in _render()  # default: plain links
    assert "data-page-swap" in _render(paginate_swap=True)  # opt-in flag turns it on


def _spend(session: Session, cost: str, *, days_ago: int = 0, **fields) -> SpendRecord:
    fields.setdefault("trigger_type", SessionOrigin.CHAT)
    fields.setdefault("model", f"m-{uuid.uuid4().hex[:6]}")
    return SpendRecord.objects.create(
        session=session,
        repo_id=session.repo_id,
        occurred_at=timezone.now() - timedelta(days=days_ago),
        cost_usd=Decimal(cost),
        **fields,
    )


def _listed(client, **params) -> list[str]:
    return [session.pk for session in client.get(reverse("session_list"), params).context["sessions"]]


@pytest.mark.django_db
class TestSessionListSpend:
    def test_cost_sort_puts_the_costliest_first_and_unpriced_sessions_last(self, logged_in_client, user):
        cheap, dear, unpriced = (_create_session(user=user) for _ in range(3))
        _spend(cheap, "1")
        _spend(dear, "2")
        _spend(dear, "3")

        assert _listed(logged_in_client, sort="cost") == [dear.pk, cheap.pk, unpriced.pk]

    def test_a_spend_period_counts_only_its_spend_and_matches_the_spend_pages_ranking(
        self, logged_in_client, user, admin_user
    ):
        old, recent = _create_session(user=user), _create_session(user=user)
        _spend(old, "50", days_ago=20)
        _spend(old, "1")
        _spend(recent, "5")
        window = window_for("7d")
        params = {"sort": "cost", "spend_from": window.start.isoformat(), "spend_to": window.end.isoformat()}

        listed = _listed(logged_in_client, **params)

        assert listed == [recent.pk, old.pk]
        top = SpendReport(admin_user, window, None, "all").top_sessions()
        assert [row["thread_id"] for row in top] == listed

    def test_each_row_shows_its_run_spend_with_priced_parts_and_without_background(self, logged_in_client, user):
        session = _create_session(user=user, title="Spent")
        _spend(session, "0.25", unpriced_tokens=10)
        _spend(session, "9", task=SpendTask.TITLE, trigger_type="")

        soup = BeautifulSoup(logged_in_client.get(reverse("session_list")).content, "html.parser")

        assert soup.select_one("[data-testid=session-cost]").get_text(strip=True) == "$0.25+"

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("spend_model", [("visible-model", "visible-model")]),
            ("spend_provider", [("anthropic", "Anthropic")]),
            ("spend_person", None),
        ],
    )
    def test_the_choices_offer_only_what_the_user_may_see(self, logged_in_client, user, other_user, key, expected):
        mine = _create_session(user=user)
        _spend(mine, "1", model="visible-model", provider_slug="anthropic", provider_name="Anthropic", user=user)
        secret = _create_session(user=other_user, repo_id="acme/secret")
        _spend(secret, "1", model="secret-model", provider_slug="secret", provider_name="Secret", user=other_user)

        response = logged_in_client.get(reverse("session_spend_choices", args=[key]), {"scope": "all"})

        items = BeautifulSoup(response.content, "html.parser").select(f"[data-filter-key={key}]")
        offered = [(item["data-value"], item["data-label"]) for item in items]
        assert offered == (expected or [(f"u{user.pk}", user.username)])

    def test_a_full_load_lists_no_spend_choices_and_names_the_current_one(self, logged_in_client, user):
        _spend(_create_session(user=user), "1", model="m1", user=user)
        _spend(_create_session(user=user), "1", model="m2", user=user)

        response = logged_in_client.get(reverse("session_list"), {"spend_person": f"u{user.pk}"})

        soup = BeautifulSoup(response.content, "html.parser")
        assert soup.select("[data-filter-key=spend_model]") == []
        assert _chip_value(soup, "spend_person") == user.username
        loader = soup.select_one("[data-filter-choices=spend_model]")
        assert loader["data-choices-url"] == reverse("session_spend_choices", args=["spend_model"])

    def test_no_spend_anywhere_shows_no_spend_controls(self, logged_in_client, user):
        soup = BeautifulSoup(logged_in_client.get(reverse("session_list")).content, "html.parser")

        assert soup.select("[data-filter-property^=spend_]") == []

    def test_an_unknown_choice_list_is_not_found(self, logged_in_client, user):
        assert logged_in_client.get(reverse("session_spend_choices", args=["status"])).status_code == 404

    def test_a_spend_drill_down_shows_one_banner(self, logged_in_client, user):
        params = {
            "scope": "all",
            "spend_from": "2026-08-22",
            "spend_to": "2026-11-19",
            "spend_repo": "srtab/daiv",
            "spend_trigger": SessionOrigin.SCHEDULE,
            "spend_task": SpendTask.TITLE,
        }

        banner = logged_in_client.get(reverse("session_list"), params).context["spend_banner"]

        assert (banner["start"], banner["end"]) == (date(2026, 8, 22), date(2026, 11, 19))
        assert (banner["repo"], banner["trigger"], banner["task"]) == (
            "srtab/daiv",
            SessionOrigin.SCHEDULE.label,
            SpendTask.TITLE.label,
        )

    def test_an_open_ended_period_keeps_the_missing_end_empty(self, logged_in_client, user):
        banner = logged_in_client.get(reverse("session_list"), {"spend_from": "2026-08-22"}).context["spend_banner"]

        assert (banner["start"], banner["end"]) == (date(2026, 8, 22), None)

    def test_clearing_the_banner_keeps_the_dropdown_filters_and_scope(self, logged_in_client, user):
        params = {
            "scope": "mine",
            "spend_from": "2026-08-22",
            "spend_repo": "srtab/daiv",
            "spend_model": "m1",
            "page": "1",
        }

        url = logged_in_client.get(reverse("session_list"), params).context["spend_banner"]["clear_url"]

        assert dict(QueryDict(urlsplit(url).query).items()) == {"scope": "mine", "spend_model": "m1"}

    def test_person_model_and_provider_alone_show_no_banner(self, logged_in_client, user):
        context = logged_in_client.get(reverse("session_list"), {"spend_model": "m1", "spend_person": "zz"}).context

        assert context["spend_banner"] is None
        assert "spend_chips" not in context

    def test_a_results_swap_reads_no_spend_choices(self, logged_in_client, user):
        session = _create_session(user=user)
        _spend(session, "1", model="m1", user=user)

        response = logged_in_client.get(
            reverse("session_list"), {"scope": "mine", "spend_model": "m1"}, HTTP_HX_REQUEST="true"
        )

        assert "spend_controls" not in response.context
        assert response.context["has_active_filters"] is True
        assert [s.pk for s in response.context["sessions"]] == [session.pk]

    def test_pagination_keeps_the_sort(self, logged_in_client, user):
        for _ in range(26):
            _create_session(user=user)

        soup = BeautifulSoup(logged_in_client.get(reverse("session_list"), {"sort": "cost"}).content, "html.parser")

        next_link = next(a for a in soup.select("a[href*='page=2']"))
        assert "sort=cost" in next_link["href"]


@pytest.mark.django_db
class TestSessionListToolbar:
    def _soup(self, client, params=None) -> BeautifulSoup:
        return BeautifulSoup(client.get(reverse("session_list"), params or {}).content, "html.parser")

    def test_the_toolbar_is_one_row_of_search_filter_scope_and_end(self, logged_in_client, user):
        soup = self._soup(logged_in_client)

        row = soup.select_one(".filter-toolbar")
        markers = row.select("#search, [data-testid=filter-open], nav.segmented-nav, [data-testid=toolbar-end]")
        assert [m.get("id") or m.get("data-testid") or "scope" for m in markers] == [
            "search",
            "filter-open",
            "scope",
            "toolbar-end",
        ]
        assert soup.select("select") == []

    def test_the_filter_menu_lists_every_filter_in_order(self, logged_in_client, user):
        names = [item["data-name"] for item in self._soup(logged_in_client).select("[data-filter-property]")]
        _spend(_create_session(user=user), "1", user=user, model="m1", provider_slug="p", provider_name="P")
        with_spend = [item["data-name"] for item in self._soup(logged_in_client).select("[data-filter-property]")]

        assert names == ["Status", "Type", "Active", "Repository"]
        assert with_spend == ["Status", "Type", "Active", "Repository", "Spent by", "Model", "Provider"]

    def test_nothing_set_shows_no_chips(self, logged_in_client, user):
        response = logged_in_client.get(reverse("session_list"))

        assert _shown_chips(BeautifulSoup(response.content, "html.parser")) == []
        assert response.context["active_filter_count"] == 0

    def test_each_set_filter_shows_one_chip_with_its_value(self, logged_in_client, user):
        _spend(_create_session(user=user), "1", user=user, model="m1")
        params = {"status": "FAILED", "trigger": "chat", "range": "7d", "repo": "group/project", "spend_model": "m1"}

        response = logged_in_client.get(reverse("session_list"), params)

        soup = BeautifulSoup(response.content, "html.parser")
        assert _shown_chips(soup) == ["status", "trigger", "time", "repo", "spend_model"]
        values = [_chip_value(soup, key) for key in _shown_chips(soup)]
        assert values == ["Failed", str(SessionOrigin.CHAT.label), "Last 7 days", "group/project", "m1"]
        assert response.context["active_filter_count"] == 5
        assert soup.select_one("[data-testid=filter-open] .filter-badge").get_text(strip=True) == "5"

    def test_a_spent_by_chip_names_the_person(self, logged_in_client, user):
        _spend(_create_session(user=user), "1", user=user)

        soup = self._soup(logged_in_client, {"spend_person": f"u{user.pk}"})

        assert _chip_value(soup, "spend_person") == user.username

    def test_deep_link_chips_join_the_chip_row(self, logged_in_client, user):
        soup = self._soup(logged_in_client, {"repo": "group/project", "mr": "7"})

        assert _shown_chips(soup) == ["repo", "mr"]
        cleared = QueryDict(urlsplit(soup.select_one("[data-chip=repo] .filter-chip__x")["href"]).query)
        assert "repo" not in cleared
        assert "mr" not in cleared
        assert soup.select_one("[data-chip=mr] .filter-chip__open") is None
        assert soup.select_one(".filter-clear").get_text(strip=True) == "Clear all"

    def test_the_archived_list_says_so_with_a_chip(self, logged_in_client, user):
        archived = self._soup(logged_in_client, {"archived": "true"})
        included = self._soup(logged_in_client, {"archived": "all"})

        assert _shown_chips(archived) == ["archived"]
        assert "Archived sessions" in archived.select_one("[data-chip=archived]").get_text()
        assert _shown_chips(included) == ["archived_included"]

    def test_settable_chips_reopen_their_filter(self, logged_in_client, user):
        soup = self._soup(logged_in_client, {"status": "FAILED"})

        assert soup.select_one("[data-chip=status] .filter-chip__open")["@click"] == "openFilter('status', $el)"

    def test_the_repository_panel_loads_its_choices_when_opened(self, logged_in_client, user):
        soup = self._soup(logged_in_client, {"repo": "group/project"})

        loader = soup.select_one("[data-filter-panel=repo] [data-filter-choices=repo]")
        assert loader["data-choices-url"] == reverse("session_repo_choices")
        assert [item["data-value"] for item in loader.select("[data-filter-key=repo]")] == ["group/project"]

    def test_sort_shows_most_recent_without_a_sort_param(self, logged_in_client, user):
        toggle = self._soup(logged_in_client).select_one("[data-testid=filter-sort]")

        assert toggle.get_text(" ", strip=True) == "Sort: Most recent"

    def test_the_spend_drill_down_reads_as_one_sentence(self, logged_in_client, user):
        params = {"scope": "all", "spend_from": "2026-08-22", "spend_to": "2026-11-19", "spend_repo": "srtab/daiv"}

        soup = self._soup(logged_in_client, params)

        assert " ".join(soup.select_one(".filter-context__text").get_text(" ").split()) == (
            "From Spend · Sessions with spend between 22 Aug 2026 and 19 Nov 2026 on srtab/daiv"
        )
        assert _shown_chips(soup) == []

    def test_an_open_ended_spend_period_reads_since(self, logged_in_client, user):
        soup = self._soup(logged_in_client, {"spend_from": "2026-08-22"})

        assert " ".join(soup.select_one(".filter-context__text").get_text(" ").split()) == (
            "From Spend · Sessions with spend since 22 Aug 2026"
        )

    def test_a_set_model_with_slashes_shows_in_its_chip(self, logged_in_client, user):
        model = "openrouter:z-ai/glm-5.2"
        _spend(_create_session(user=user), "1", user=user, model=model)

        soup = self._soup(logged_in_client, {"spend_model": model})

        assert _shown_chips(soup) == ["spend_model"]
        assert _chip_value(soup, "spend_model") == model

    def test_archived_is_a_toggle_inside_its_own_control(self, logged_in_client, user):
        toggle = self._soup(logged_in_client).select_one("[data-testid=filter-archived]")

        assert "filter-control__toggle" in toggle["class"]
        assert "filter-control" not in toggle["class"]
        assert "filter-control" in toggle.parent["class"]

    def test_the_custom_range_row_wraps_inside_the_time_menu(self, logged_in_client, user):
        from tests.unit_tests.test_list_pages_on_tokens import _rules_for

        row = self._soup(logged_in_client).select_one("[aria-label='From date']").parent

        assert "filter-range" in row["class"]
        assert any("flex-wrap" in body for body in _rules_for("filter-range"))

    def test_clear_all_keeps_the_scope_and_a_chosen_sort(self, logged_in_client, user):
        soup = self._soup(logged_in_client, {"scope": "all", "status": "FAILED", "sort": "cost", "group": "status"})

        url = soup.select_one(".filter-clear")["href"]
        assert dict(QueryDict(urlsplit(url).query).items()) == {"scope": "all", "sort": "cost", "group": "status"}
        empty_state_url = soup.select_one("#session-results a[href*='sort=cost']")["href"]
        assert empty_state_url == url

    def test_a_set_spend_filter_keeps_its_chip_without_visible_spend(self, logged_in_client, user):
        soup = self._soup(logged_in_client, {"spend_model": "gone-model"})

        assert _chip_value(soup, "spend_model") == "gone-model"

    def test_a_cost_sorted_list_has_no_date_headers(self, logged_in_client, user):
        _spend(_create_session(user=user), "1")
        _spend(_create_session(user=user, archived_at=timezone.now()), "1")

        for params in ({"sort": "cost"}, {"sort": "cost", "archived": "true"}):
            for headers in ({}, {"HTTP_HX_REQUEST": "true"}):
                html = logged_in_client.get(reverse("session_list"), params, **headers).content.decode()
                assert "session-group-header" not in html, (params, headers)

    def test_a_recent_list_keeps_its_date_headers(self, logged_in_client, user):
        _create_session(user=user)

        assert "session-group-header" in logged_in_client.get(reverse("session_list")).content.decode()


def _headers(response) -> list[str]:
    soup = BeautifulSoup(response.content, "html.parser")
    return [" ".join(header.get_text(" ").split()) for header in soup.select(".session-group-header")]


@pytest.mark.django_db
class TestSessionListGrouping:
    def _with_latest(self, user, status, *, hours_ago: int = 0, **fields) -> Session:
        session = _create_session(user=user, **fields)
        if status is not None:
            _create_run(session, status=status)
        Session.objects.filter(pk=session.pk).update(last_active_at=timezone.now() - timedelta(hours=hours_ago))
        return session

    def test_group_by_status_puts_attention_first_then_recency(self, logged_in_client, user):
        no_run = self._with_latest(user, None)
        older_done = self._with_latest(user, RunStatus.SUCCESSFUL, hours_ago=5)
        newer_done = self._with_latest(user, RunStatus.SUCCESSFUL, hours_ago=1)
        failed = self._with_latest(user, RunStatus.FAILED)
        running = self._with_latest(user, RunStatus.RUNNING, hours_ago=9)
        waiting = self._with_latest(user, RunStatus.WAITING_INPUT, hours_ago=9)

        listed = _listed(logged_in_client, group="status")

        assert listed == [s.pk for s in (waiting, running, failed, newer_done, older_done, no_run)]

    def test_status_headers_count_the_whole_filtered_list(self, logged_in_client, user):
        for _ in range(26):
            self._with_latest(user, RunStatus.SUCCESSFUL, repo_id="group/project")
        self._with_latest(user, RunStatus.FAILED, repo_id="group/project")
        for _ in range(3):
            self._with_latest(user, RunStatus.SUCCESSFUL, repo_id="group/other")
        params = {"scope": "mine", "group": "status", "repo": "group/project"}

        first = logged_in_client.get(reverse("session_list"), params)
        second = logged_in_client.get(reverse("session_list"), {**params, "page": 2})

        assert _headers(first) == ["Failed 1", "Done 26"]
        assert _headers(second) == ["Done 26"]

    def test_no_run_sessions_head_their_own_group(self, logged_in_client, user):
        self._with_latest(user, None)

        assert _headers(logged_in_client.get(reverse("session_list"), {"group": "status"})) == ["No runs 1"]

    def test_group_none_has_no_headers(self, logged_in_client, user):
        self._with_latest(user, RunStatus.SUCCESSFUL)

        assert _headers(logged_in_client.get(reverse("session_list"), {"group": "none"})) == []

    def test_an_unknown_group_keeps_date_headers(self, logged_in_client, user):
        self._with_latest(user, RunStatus.SUCCESSFUL)

        response = logged_in_client.get(reverse("session_list"), {"group": "bogus"})

        assert response.status_code == 200
        assert response.context["current_group"] == ""
        assert _headers(response) == ["Today"]

    def test_cost_sort_grouped_by_status_orders_by_cost_inside_each_group(self, logged_in_client, user):
        cheap = self._with_latest(user, RunStatus.FAILED)
        dear = self._with_latest(user, RunStatus.FAILED, hours_ago=3)
        _spend(cheap, "1")
        _spend(dear, "5")

        response = logged_in_client.get(reverse("session_list"), {"sort": "cost", "group": "status"})

        assert [s.pk for s in response.context["sessions"]] == [dear.pk, cheap.pk]
        assert _headers(response) == ["Failed 2"]

    def test_status_headers_render_in_the_results_fragment(self, logged_in_client, user):
        self._with_latest(user, RunStatus.RUNNING)

        response = logged_in_client.get(reverse("session_list"), {"group": "status"}, HTTP_HX_REQUEST="true")

        assert _headers(response) == ["Running 1"]


@pytest.mark.django_db
class TestSessionRepoChoices:
    def _values(self, response) -> list[str]:
        soup = BeautifulSoup(response.content, "html.parser")
        return [item["data-value"] for item in soup.select("[data-filter-key=repo]")]

    def test_repo_choices_follow_the_scope(self, logged_in_client, admin_client, user, other_user):
        _create_session(user=user, repo_id="a/one")
        _create_session(user=user, repo_id="a/two")
        _create_session(user=user, repo_id="a/two", archived_at=timezone.now())
        _create_session(user=user, repo_id="")
        _create_session(user=other_user, repo_id="b/three")
        url = reverse("session_repo_choices")

        assert self._values(logged_in_client.get(url, {"scope": "mine"})) == ["a/one", "a/two"]
        assert self._values(admin_client.get(url, {"scope": "all"})) == ["a/one", "a/two", "b/three"]

    def test_repo_choices_keep_the_list_query_and_mark_the_current_one(self, logged_in_client, user):
        _create_session(user=user, repo_id="a/one")
        _create_session(user=user, repo_id="a/two")

        response = logged_in_client.get(
            reverse("session_repo_choices"), {"scope": "all", "status": "FAILED", "repo": "a/two"}
        )

        soup = BeautifulSoup(response.content, "html.parser")
        item = soup.select_one("[data-filter-key=repo][data-value='a/one']")
        assert "filter-menu-item--active" in soup.select_one("[data-value='a/two']")["class"]
        assert dict(QueryDict(urlsplit(item["href"]).query).items()) == {
            "scope": "all",
            "status": "FAILED",
            "repo": "a/one",
        }

    def test_repo_choices_need_sign_in(self, client):
        response = client.get(reverse("session_repo_choices"))

        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]
