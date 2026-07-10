from __future__ import annotations

import re
import uuid

from django.test import Client
from django.urls import reverse

import pytest
from sessions.models import Run, RunStatus, Session, SessionOrigin

from accounts.models import User


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


def _create_run(session: Session, **kwargs) -> Run:
    defaults = {
        "session": session,
        "trigger_type": SessionOrigin.SCHEDULE,
        "repo_id": session.repo_id,
        "status": RunStatus.SUCCESSFUL,
    }
    defaults.update(kwargs)
    return Run.objects.create(**defaults)


@pytest.mark.django_db
class TestSessionListView:
    def test_unauthenticated_redirects_to_login(self):
        response = Client().get(reverse("session_list"))
        assert response.status_code == 302
        assert "/accounts/login/" in response.url

    def test_authenticated_user_can_access(self, logged_in_client, user):
        response = logged_in_client.get(reverse("session_list"))
        assert response.status_code == 200

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
        assert response.context["current_range_label"] == "Last 7 days"
        assert response.context["has_active_filters"] is True

    def test_has_active_filters_true_when_q_set(self, logged_in_client, user):
        _create_session(user=user)
        response = logged_in_client.get(reverse("session_list"), {"q": "abc"})
        assert response.context["has_active_filters"] is True

    def test_runs_are_prefetched_newest_first(self, logged_in_client, user, django_assert_num_queries):
        session = _create_session(user=user)
        _create_run(session, status=RunStatus.SUCCESSFUL)
        newest = _create_run(session, status=RunStatus.RUNNING)
        response = logged_in_client.get(reverse("session_list"))
        row = next(s for s in response.context["sessions"] if s.pk == session.pk)
        with django_assert_num_queries(0):
            latest = list(row.runs.all())[0]  # served from the prefetch cache — no query
        assert latest.pk == newest.pk

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
            "accounts/_pagination.html",
            {"is_paginated": True, "page_obj": _Page(), "request": rf.get("/x/?foo=bar"), **extra},
        )

    assert "data-page-swap" not in _render()  # default: plain links
    assert "data-page-swap" in _render(paginate_swap=True)  # opt-in flag turns it on
