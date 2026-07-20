"""Markup-level tests for the Review Console shell (Story 2.1).

Mirrors ``test_sidebar.py``: assert on ``response.content`` bytes via ``data-testid``
hooks, reusing the ``admin_client`` / ``member_client`` fixtures from ``conftest.py``.
The console ships as a structural/visual substrate only — no region data, no
job-creation launcher in the console content.
"""

import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.template.loader import get_template, render_to_string
from django.test import Client
from django.urls import reverse
from django.utils import timezone

import pytest
from notifications.choices import EventType
from notifications.models import Notification
from sessions.models import EnvelopeStatus, Run, RunEnvelope, RunStatus, Session, SessionOrigin

from accounts.models import Role
from accounts.views import get_velocity_data
from codebase.clients import RepoClient
from codebase.models import MergeMetric, PlatformType

# Repo root: tests/unit_tests/accounts/ -> parents[3].
REPO_ROOT = Path(__file__).resolve().parents[3]
INPUT_CSS = REPO_ROOT / "daiv" / "static_src" / "css" / "input.css"


def _make_merge_metric(*, iid=1, daiv_commits=1, total_commits=2, lines_added=10, lines_removed=3, title=""):
    """Create a ``MergeMetric`` row directly (no factory exists).

    Fills the required non-default fields (``merged_at``/``target_branch``/``source_branch``/
    ``platform``); ``merged_at`` is *now* so the row survives the default "today" period filter.
    Distinct ``iid`` values keep the ``(repo_id, merge_request_iid, platform)`` uniqueness happy.
    """
    return MergeMetric.objects.create(
        repo_id="daiv/test",
        merge_request_iid=iid,
        title=title,
        lines_added=lines_added,
        lines_removed=lines_removed,
        total_commits=total_commits,
        daiv_commits=daiv_commits,
        merged_at=timezone.now(),
        target_branch="main",
        source_branch="feature/x",
        platform=PlatformType.GITLAB,
    )


@pytest.mark.django_db
class TestConsoleRegions:
    def test_three_region_containers_render(self, member_client):
        response = member_client.get(reverse("dashboard"))
        assert response.status_code == 200
        assert b'data-testid="console-hero"' in response.content
        assert b'data-testid="console-queue"' in response.content
        assert b'data-testid="console-feed"' in response.content

    def test_queue_count_container_is_polite_live_region(self, member_client):
        response = member_client.get(reverse("dashboard"))
        assert b'data-testid="console-queue-count"' in response.content
        assert b'aria-live="polite"' in response.content


@pytest.mark.django_db
class TestNoLauncherInConsoleContent:
    """FR-3: the console content region exposes no free-form job-creation control.

    The global sidebar "New →" CTA (``nav-new-cta``) is shared chrome and MUST stay
    (D2) — so we assert on the console body FRAGMENT, which excludes the sidebar.
    """

    def test_console_body_fragment_has_no_launcher(self, member_client):
        response = member_client.get(reverse("dashboard"), HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        # The console body renders the regions ...
        assert b'data-testid="console-hero"' in response.content
        # ... but carries no free-form launcher or job-creation link.
        assert b"New Job" not in response.content
        assert reverse("session_new").encode() not in response.content

    def test_global_new_cta_stays_on_full_page(self, member_client):
        """Guards against over-zealous removal: the shared CTA must remain (D2)."""
        response = member_client.get(reverse("dashboard"))
        assert b'data-testid="nav-new-cta"' in response.content


@pytest.mark.django_db
class TestHtmxFragment:
    def test_htmx_request_returns_body_fragment_only(self, member_client):
        response = member_client.get(reverse("dashboard"), HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        # Fragment carries the console regions ...
        assert b'data-testid="console-feed"' in response.content
        # ... and none of the shell chrome.
        assert b'data-testid="app-sidebar"' not in response.content
        assert b'data-testid="app-user-menu"' not in response.content
        assert b"<html" not in response.content

    def test_full_page_returns_shell_chrome(self, member_client):
        response = member_client.get(reverse("dashboard"))
        assert b'data-testid="app-sidebar"' in response.content
        assert b'data-testid="app-user-menu"' in response.content


@pytest.mark.django_db
class TestAdminNavVisibility:
    def test_admin_sees_admin_group(self, admin_client):
        response = admin_client.get(reverse("dashboard"))
        assert b'data-testid="nav-admin-group"' in response.content

    def test_member_does_not_see_admin_group(self, member_client):
        response = member_client.get(reverse("dashboard"))
        assert b'data-testid="nav-admin-group"' not in response.content


@pytest.mark.django_db
class TestVisualFoundationWiring:
    def test_token_classes_present_in_markup(self, member_client):
        response = member_client.get(reverse("dashboard"))
        # Body uses the ground/text tokens; the shell uses surface + border tokens.
        assert b"bg-ground" in response.content
        assert b"text-text" in response.content
        assert b"bg-surface-1" in response.content
        assert b"border-border" in response.content

    def test_font_sans_applied_on_body(self, member_client):
        response = member_client.get(reverse("dashboard"))
        assert b"font-sans" in response.content

    def test_skeleton_markup_present(self, member_client):
        response = member_client.get(reverse("dashboard"))
        assert b"skeleton" in response.content

    def test_range_switcher_and_bottom_tab_bar_present(self, member_client):
        response = member_client.get(reverse("dashboard"))
        assert b'data-testid="range-switcher"' in response.content
        assert b'data-testid="mobile-tab-bar"' in response.content
        # Exactly four bottom-tab labels; touch targets sized to >= 44px.
        assert b"min-h-[44px]" in response.content

    def test_range_switcher_absent_on_non_console_pages(self, member_client):
        """The range-switcher is console-only: it lives in the dashboard's
        ``topbar_start`` block, so other pages sharing ``base_app.html`` (MCP
        servers, sandbox envs, users, sessions, ...) must NOT show it."""
        response = member_client.get(reverse("session_list"))
        assert response.status_code == 200
        assert b'data-testid="app-sidebar"' in response.content  # it IS a base_app shell page
        assert b'data-testid="range-switcher"' not in response.content

    def test_no_outfit_google_fonts_cdn(self, member_client):
        response = member_client.get(reverse("dashboard"))
        assert b"fonts.googleapis.com" not in response.content
        assert b"Outfit" not in response.content


class TestCssTokenLayer:
    """Source-level checks on the Tailwind v4 token layer / self-hosted fonts."""

    def test_input_css_declares_color_tokens(self):
        css = INPUT_CSS.read_text(encoding="utf-8")
        assert "--color-ground: #0d1117" in css
        assert "--color-accent:" in css
        assert "--color-status-attn: #38bdf8" in css  # cyan, not violet

    def test_input_css_self_hosts_geist(self):
        css = INPUT_CSS.read_text(encoding="utf-8")
        assert "@font-face" in css
        assert '"Geist"' in css
        assert '"Geist Mono"' in css
        assert "font-display: swap" in css
        assert "geist-latin-wght-normal.woff2" in css

    def test_input_css_has_teal_focus_ring(self):
        css = INPUT_CSS.read_text(encoding="utf-8")
        assert ":focus-visible" in css
        assert "var(--color-focus)" in css

    def test_input_css_respects_reduced_motion(self):
        css = INPUT_CSS.read_text(encoding="utf-8")
        assert "prefers-reduced-motion: reduce" in css

    def test_input_css_flat_elevation_single_overlay_shadow(self):
        css = INPUT_CSS.read_text(encoding="utf-8")
        assert "--shadow-overlay:" in css


class TestI18nExternalization:
    """A shell label must be i18n-wrapped (not a bare hard-coded string)."""

    def test_console_body_label_is_translated(self):
        src = Path(get_template("accounts/_console_body.html").origin.name).read_text(encoding="utf-8")
        assert '{% translate "Needs-me queue" %}' in src

    def test_range_switcher_labels_are_translated(self):
        # The range-switcher now lives in the dashboard's topbar_start block
        # (console-only), not in the shared base_app.html shell.
        src = Path(get_template("accounts/dashboard.html").origin.name).read_text(encoding="utf-8")
        assert '{% translate "All time" %}' in src
        # aria-label wraps the string in single quotes (double-quoted attribute).
        assert 'translate "Time range"' in src or "translate 'Time range'" in src

    @pytest.mark.django_db
    def test_translated_label_renders(self, member_client):
        # Test settings render en (canonical), so the source string appears verbatim.
        response = member_client.get(reverse("dashboard"))
        assert b"Needs-me queue" in response.content


# ---------------------------------------------------------------------------
# Story 2.2 — Personal-by-default with the admin Manager Lens
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPersonalByDefault:
    """AC1: the default ``/dashboard/`` carries NO org/aggregate content for anyone."""

    def test_default_has_no_org_content_for_member(self, member_client):
        response = member_client.get(reverse("dashboard"))
        assert response.status_code == 200
        assert "velocity" not in response.context
        assert "total_users" not in response.context
        assert b'data-testid="manager-lens"' not in response.content
        assert b"Code Velocity" not in response.content

    def test_default_has_no_org_content_for_admin_even_with_merges(self, admin_client):
        # Even with org merges present, the personal default surfaces none of it (admins too).
        _make_merge_metric()
        response = admin_client.get(reverse("dashboard"))
        assert response.status_code == 200
        assert "velocity" not in response.context
        assert "total_users" not in response.context
        # The relocated velocity/attribution markup must not leak onto the default.
        assert b'data-testid="manager-lens"' not in response.content
        assert b"Code Velocity" not in response.content
        assert b"DAIV attribution" not in response.content


@pytest.mark.django_db
class TestManagerLensToggle:
    """AC2: toggle present for admins, absent (not shown-then-blocked) for non-admins."""

    def test_toggle_present_for_admin(self, admin_client):
        response = admin_client.get(reverse("dashboard"))
        assert b'data-testid="manager-lens-toggle"' in response.content

    def test_toggle_absent_for_member(self, member_client):
        response = member_client.get(reverse("dashboard"))
        assert b'data-testid="manager-lens-toggle"' not in response.content

    def test_toggle_present_on_lens_page_for_admin(self, admin_client):
        response = admin_client.get(reverse("manager_lens"))
        assert b'data-testid="manager-lens-toggle"' in response.content

    def test_toggle_wires_both_console_routes(self, admin_client):
        response = admin_client.get(reverse("dashboard"))
        # The segmented control HTMX-swaps between the two console routes with a plain-link fallback.
        assert reverse("manager_lens").encode() in response.content
        assert b'hx-target="#console-main"' in response.content
        assert b'hx-push-url="true"' in response.content


@pytest.mark.django_db
class TestManagerLensContent:
    """AC3: the Lens shows the relocated org velocity + DAIV-attribution."""

    def test_admin_sees_relocated_velocity(self, admin_client):
        _make_merge_metric(daiv_commits=1, total_commits=2, lines_added=42, lines_removed=7)
        response = admin_client.get(reverse("manager_lens"), {"period": "all"})
        assert response.status_code == 200
        assert response.context["velocity"] is not None
        assert response.context["velocity"]["total_merges"] == 1
        assert response.context["total_users"] >= 1
        assert b'data-testid="manager-lens"' in response.content
        assert b'data-testid="lens-total-merges"' in response.content
        assert b"Code Velocity" in response.content
        assert b"DAIV attribution" in response.content
        # Data present -> no cold-load skeleton.
        assert b'data-testid="manager-lens-skeleton"' not in response.content

    def test_default_shows_cumulative_velocity_not_scoped_to_today(self, admin_client):
        # Regression guard: the Lens defaults to the cumulative window, so an org whose only
        # merges are older than "today" still shows real velocity — never the cold-load skeleton.
        old = _make_merge_metric(iid=99)
        MergeMetric.objects.filter(pk=old.pk).update(merged_at=timezone.now() - timedelta(days=40))
        response = admin_client.get(reverse("manager_lens"))  # no ?period= -> cumulative default
        assert response.status_code == 200
        assert response.context["velocity"] is not None
        assert response.context["velocity"]["total_merges"] == 1
        assert b'data-testid="manager-lens-skeleton"' not in response.content

    def test_velocity_uses_shared_module_function(self, admin_client):
        # The Lens reads through the extracted module-level ``get_velocity_data`` (AD-10: MergeMetric).
        _make_merge_metric(iid=1, daiv_commits=2, total_commits=2)
        _make_merge_metric(iid=2, daiv_commits=0, total_commits=3)
        response = admin_client.get(reverse("manager_lens"), {"period": "all"})
        velocity = response.context["velocity"]
        assert velocity["total_merges"] == 2
        assert velocity["daiv_merges"] == 1  # only the row with daiv_commits > 0


@pytest.mark.django_db
class TestManagerLensColdLoad:
    """AC5: zero MergeMetric rows -> cold-load skeleton, never a zero dressed as a fact."""

    def test_cold_load_shows_skeleton_not_zero(self, admin_client):
        assert MergeMetric.objects.count() == 0
        response = admin_client.get(reverse("manager_lens"))
        assert response.status_code == 200
        assert response.context["velocity"] is None
        assert b'data-testid="manager-lens-skeleton"' in response.content
        # The block is NOT hidden, and no zero reading is rendered as a fact.
        assert b'data-testid="manager-lens"' in response.content
        assert b'data-testid="lens-total-merges"' not in response.content
        assert b"merges into default branches" not in response.content


@pytest.mark.django_db
class TestManagerLensAccessDenial:
    """AC6: a non-admin hitting the route directly is denied SERVER-SIDE (403)."""

    def test_member_gets_403(self, member_client):
        response = member_client.get(reverse("manager_lens"))
        assert response.status_code == 403

    def test_member_gets_403_even_over_htmx(self, member_client):
        response = member_client.get(reverse("manager_lens"), HTTP_HX_REQUEST="true")
        assert response.status_code == 403

    def test_anonymous_redirected_to_login(self):
        response = Client().get(reverse("manager_lens"))
        assert response.status_code == 302
        assert "/accounts/login/" in response.url


@pytest.mark.django_db
class TestManagerLensNonPersistence:
    """AC4: using the toggle persists nothing; the next visit lands personal."""

    def test_lens_visit_writes_no_state_and_default_stays_personal(self, admin_client, admin_user):
        keys_before = set(admin_client.session.keys())

        first = admin_client.get(reverse("manager_lens"))
        assert first.status_code == 200

        # No app-level state was persisted: only Django's own framework keys (``_csrftoken`` etc.,
        # all underscore-prefixed) may appear — no surface/lens preference of ANY name is written.
        new_keys = set(admin_client.session.keys()) - keys_before
        app_keys = {k for k in new_keys if not k.startswith("_")}
        assert not app_keys, f"Manager Lens visit unexpectedly wrote app session keys: {app_keys}"

        # Returning to the console lands on the PERSONAL view — no org content flips the default.
        second = admin_client.get(reverse("dashboard"))
        assert second.status_code == 200
        assert "velocity" not in second.context
        assert b'data-testid="manager-lens"' not in second.content

        # No user field was flipped to remember the surface.
        admin_user.refresh_from_db()
        assert admin_user.role == Role.ADMIN


@pytest.mark.django_db
class TestManagerLensHtmxFragment:
    """The partial-on-HTMX idiom: HX-Request -> fragment only (no base_app chrome)."""

    def test_htmx_returns_fragment_only(self, admin_client):
        _make_merge_metric()
        response = admin_client.get(reverse("manager_lens"), {"period": "all"}, HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        # Fragment carries the Lens body ...
        assert b'data-testid="manager-lens"' in response.content
        # ... and none of the shell chrome.
        assert b'data-testid="app-sidebar"' not in response.content
        assert b'data-testid="app-user-menu"' not in response.content
        assert b"<html" not in response.content

    def test_full_page_returns_shell_chrome(self, admin_client):
        _make_merge_metric()
        response = admin_client.get(reverse("manager_lens"), {"period": "all"})
        assert b'data-testid="app-sidebar"' in response.content
        assert b'data-testid="app-user-menu"' in response.content


class TestManagerLensI18n:
    """All new user-facing strings are i18n-wrapped (not bare hard-coded)."""

    def test_toggle_labels_are_translated(self):
        src = Path(get_template("accounts/_manager_lens_toggle.html").origin.name).read_text(encoding="utf-8")
        assert '{% translate "Personal" %}' in src
        assert '{% translate "Manager Lens" %}' in src

    def test_lens_strings_are_translated(self):
        src = Path(get_template("accounts/_manager_lens.html").origin.name).read_text(encoding="utf-8")
        assert '{% translate "Org impact" %}' in src
        assert '{% translate "MRs involving DAIV" %}' in src
        # The cold-load aria-busy label (single-quoted inside the double-quoted attribute).
        assert "{% translate 'Computing org impact…' %}" in src


# ---------------------------------------------------------------------------
# Story 3.1 — The personal Throughput Hero + wired range switcher
# ---------------------------------------------------------------------------

# ``merged_at`` is *now* in ``_make_merge_metric``; distinct ``iid`` keeps the
# (repo_id, merge_request_iid, platform) uniqueness happy. The Hero counts a merge only when a
# ``Run`` in one of the USER's own sessions shares its (repo_id, merge_request_iid) — the value-based
# ``MergeMetric ⋈ Run`` attribution join. This helper builds that owning Session+Run inline (no
# factory exists), mirroring how ``_make_merge_metric`` builds the MergeMetric directly.


def _make_shipping_run(user, *, iid, repo_id="daiv/test", web_url=""):
    """Create a Session owned by ``user`` + a Run sharing ``(repo_id, iid)`` — the DAIV-opened,
    by-this-user attribution the Hero join asserts. Pairs with a ``_make_merge_metric(iid=iid)``.

    ``web_url`` carries onto ``Run.merge_request_web_url`` (the best-effort MR out-link the 3.2
    breakdown surfaces through the join; blank exercises the AC11 degrade-to-session-link path)."""
    session = Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.MR_WEBHOOK, repo_id=repo_id, user=user
    )
    return Run.objects.create(
        session=session,
        trigger_type=SessionOrigin.MR_WEBHOOK,
        status=RunStatus.SUCCESSFUL,
        repo_id=repo_id,
        merge_request_iid=iid,
        merge_request_web_url=web_url,
    )


@pytest.mark.django_db
class TestHeroCountAndAttribution:
    """AC2/AC3: 'changes shipped' = merged MRs opened by DAIV for THIS user (MergeMetric ⋈ Run)."""

    def test_counts_only_merges_with_a_matching_user_run(self, member_client, member_user):
        # iid=1 → member's own run (counted); iid=2 → human MR, no run (excluded).
        _make_merge_metric(iid=1)
        _make_shipping_run(member_user, iid=1)
        _make_merge_metric(iid=2)  # no run → human-authored → excluded

        response = member_client.get(reverse("dashboard"))
        assert response.status_code == 200
        hero = response.context["hero"]
        assert hero is not None
        assert hero["all_time"] == 1
        assert hero["this_range"]["total_merges"] == 1
        assert b'data-testid="hero-headline"' in response.content

    def test_another_users_merge_is_excluded(self, member_client, member_user, admin_user):
        # A merge matched only by ANOTHER user's run must not count for member.
        _make_merge_metric(iid=3)
        _make_shipping_run(admin_user, iid=3)

        response = member_client.get(reverse("dashboard"))
        # member has zero matched merges → honest empty state, not a 0-headline.
        assert response.context["hero"] is None
        assert b'data-testid="hero-empty"' in response.content


@pytest.mark.django_db
class TestHeroAdminPersonalScope:
    """AC10: the admin Hero counts only the admin's OWN merges — never the by_owner/.all() short-circuit."""

    def test_admin_hero_is_personal_not_org_wide(self, admin_client, admin_user, member_user):
        _make_merge_metric(iid=1)
        _make_shipping_run(admin_user, iid=1)  # admin's own → counted
        _make_merge_metric(iid=2)
        _make_shipping_run(member_user, iid=2)  # another user's → excluded for admin
        _make_merge_metric(iid=3)  # human MR, no run → excluded

        response = admin_client.get(reverse("dashboard"))
        hero = response.context["hero"]
        assert hero is not None
        # Personal scope: only the admin's own 1 merge, NOT the org-wide 3 (or 2).
        assert hero["all_time"] == 1

    def test_personal_by_default_still_holds_with_hero(self, admin_client, admin_user):
        # AC10 belt-and-braces: even with a live personal Hero, the default carries no org content.
        _make_merge_metric(iid=1)
        _make_shipping_run(admin_user, iid=1)
        response = admin_client.get(reverse("dashboard"))
        assert "velocity" not in response.context
        assert "total_users" not in response.context
        assert b'data-testid="manager-lens"' not in response.content


@pytest.mark.django_db
class TestHeroRangeScopingAndDelta:
    """AC1/AC6 + D3/D4: range scopes the headline, delta vs the prior equal window, hidden for 'all'."""

    def _seed_windows(self, user):
        # this-week ×2 (now), prev-window ×1 (10d ago), older ×1 (40d ago); all owned by ``user``.
        for iid in (1, 2, 3, 4):
            _make_merge_metric(iid=iid)
            _make_shipping_run(user, iid=iid)
        MergeMetric.objects.filter(merge_request_iid=3).update(merged_at=timezone.now() - timedelta(days=10))
        MergeMetric.objects.filter(merge_request_iid=4).update(merged_at=timezone.now() - timedelta(days=40))

    def test_default_view_resolves_to_7d_this_week_with_delta(self, member_client, member_user):
        self._seed_windows(member_user)
        response = member_client.get(reverse("dashboard"))  # no ?period= → D4 default
        assert response.context["current_period"] == "7d"
        hero = response.context["hero"]
        assert hero["this_range"]["total_merges"] == 2  # the two this-week merges
        assert hero["delta"] == 1  # 2 this week − 1 in the preceding 7-day window
        assert hero["all_time"] == 4
        assert b"this week" in response.content
        assert b'data-testid="hero-delta"' in response.content

    def test_delta_uses_equal_length_windows(self, member_client, member_user):
        # AC1/D3 regression: the current "7d" window is [today−7, today] = 8 inclusive days, so the
        # preceding window must also span 8 days. With a flat one-merge-per-day cadence a 7-day
        # previous window would report a spurious +1; an equal 8-day window reports 0.
        for offset in range(16):  # today, today−1, … today−15 (one merge per day)
            iid = offset + 1
            _make_merge_metric(iid=iid)
            _make_shipping_run(member_user, iid=iid)
            MergeMetric.objects.filter(merge_request_iid=iid).update(merged_at=timezone.now() - timedelta(days=offset))
        hero = member_client.get(reverse("dashboard"), {"period": "7d"}).context["hero"]
        assert hero["this_range"]["total_merges"] == 8  # offsets 0..7
        assert hero["delta"] == 0  # preceding equal 8-day window (offsets 8..15) also holds 8

    def test_30d_range_label_and_scope(self, member_client, member_user):
        self._seed_windows(member_user)
        response = member_client.get(reverse("dashboard"), {"period": "30d"})
        hero = response.context["hero"]
        assert hero["this_range"]["total_merges"] == 3  # excludes the 40-day-old merge
        assert b"in the last 30 days" in response.content

    def test_all_time_range_has_no_delta_chip(self, member_client, member_user):
        self._seed_windows(member_user)
        response = member_client.get(reverse("dashboard"), {"period": "all"})
        hero = response.context["hero"]
        assert hero["this_range"]["total_merges"] == 4  # every merge
        assert hero["delta"] is None
        assert b'data-testid="hero-delta"' not in response.content

    def test_htmx_fragment_rescopes_hero(self, member_client, member_user):
        self._seed_windows(member_user)
        response = member_client.get(reverse("dashboard"), {"period": "all"}, HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        assert b'data-testid="hero-headline"' in response.content
        assert b'data-testid="app-sidebar"' not in response.content  # fragment, not the shell
        assert response.context["hero"]["this_range"]["total_merges"] == 4


@pytest.mark.django_db
class TestHeroOdometerInvariance:
    """AC1: the all-time odometer is invariant to the selected range."""

    def test_all_time_identical_across_ranges(self, member_client, member_user):
        for iid in (1, 2, 3):
            _make_merge_metric(iid=iid)
            _make_shipping_run(member_user, iid=iid)
        MergeMetric.objects.filter(merge_request_iid=3).update(merged_at=timezone.now() - timedelta(days=40))

        seven = member_client.get(reverse("dashboard"), {"period": "7d"}).context["hero"]["all_time"]
        thirty = member_client.get(reverse("dashboard"), {"period": "30d"}).context["hero"]["all_time"]
        assert seven == thirty == 3


@pytest.mark.django_db
class TestHeroDefaultPeriod:
    """D4: the personal console default period is now '7d' (was 'today')."""

    def test_default_period_is_7d(self, member_client):
        response = member_client.get(reverse("dashboard"))
        assert response.context["current_period"] == "7d"


class TestHeroEstimateDemotion:
    """AC4/D1: no estimate line in v1; a truthy estimate renders demoted (never fact-styled)."""

    def _render(self, *, estimate):
        hero = {"this_range": {"total_merges": 5}, "period": "7d", "delta": 1, "all_time": 100, "estimate": estimate}
        return render_to_string("accounts/_hero.html", {"hero": hero})

    def test_v1_estimate_none_renders_no_estimate_line(self):
        html = self._render(estimate=None)
        assert 'data-testid="hero-estimate"' not in html

    def test_truthy_estimate_is_visually_demoted(self):
        html = self._render(estimate=48)
        assert 'data-testid="hero-estimate"' in html
        start = html.index('data-testid="hero-estimate"')
        block = html[start : html.index("</p>", start)]
        assert "~48 dev-hours saved" in html
        # Demotion: italic + faint + dotted underline + an ``est.`` tag ...
        assert "italic" in block
        assert "text-text-faint" in block
        assert "decoration-dotted" in block
        assert "est." in block
        # ... and NEVER the fact styling (solid white display mono).
        assert "text-text-strong" not in block


@pytest.mark.django_db
class TestHeroSlackLine:
    """AC5: the copyable Slack one-liner sits in its own overflow-x-auto inset with a Copy control."""

    def test_slack_line_overflow_and_copy_control(self, member_client, member_user):
        _make_merge_metric(iid=1)
        _make_shipping_run(member_user, iid=1)
        response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert 'data-testid="hero-slack"' in content
        # The inset owns its overflow container so a long line never scrolls the body.
        slack_start = content.index('data-testid="hero-slack"')
        assert "overflow-x-auto" in content[slack_start - 120 : slack_start + 120]
        assert 'data-testid="hero-copy"' in content
        # The clipboard idiom's json_script payload is present with the referenced id.
        assert 'id="hero-slack-line"' in content
        assert "DAIV shipped 1 changes this week (1 all-time)" in content


@pytest.mark.django_db
class TestHeroEmptyState:
    """AC13: nothing shipped all-time → honest empty state, never a 0-headline fact."""

    def test_empty_hero_is_honest(self, member_client):
        response = member_client.get(reverse("dashboard"))
        assert response.context["hero"] is None
        assert b'data-testid="hero-empty"' in response.content
        assert b"No changes shipped yet" in response.content
        # No 0 dressed up as a headline fact.
        assert b'data-testid="hero-headline"' not in response.content


@pytest.mark.django_db
class TestGetVelocityDataQuerysetRefactor:
    """AC11: one shared counting body — the queryset arg scopes it; the default stays org-wide."""

    def test_default_is_org_wide_and_queryset_scopes(self, member_user):
        _make_merge_metric(iid=1)
        _make_merge_metric(iid=2)
        # Default (no queryset) aggregates every MergeMetric — protects the org-wide ManagerLensView.
        assert get_velocity_data(None)["total_merges"] == 2
        # A passed subset aggregates only that subset — the personal Hero's user-scoped path.
        subset = MergeMetric.objects.filter(merge_request_iid=1)
        assert get_velocity_data(None, queryset=subset)["total_merges"] == 1

    def test_empty_queryset_returns_none(self):
        assert get_velocity_data(None, queryset=MergeMetric.objects.none()) is None


@pytest.mark.django_db
class TestHeroReadOnly:
    """AC12: rendering the Hero performs read-only queries — no row is created or mutated."""

    def test_render_writes_nothing(self, member_client, member_user):
        _make_merge_metric(iid=1)
        _make_shipping_run(member_user, iid=1)
        before = (MergeMetric.objects.count(), Run.objects.count(), Session.objects.count())
        member_client.get(reverse("dashboard"))
        member_client.get(reverse("dashboard"), {"period": "all"})
        after = (MergeMetric.objects.count(), Run.objects.count(), Session.objects.count())
        assert before == after


class TestHeroI18n:
    """AC9: every new Hero string is {% translate %}/{% blocktranslate %}-wrapped (no bare literal)."""

    def test_hero_strings_are_translated(self):
        src = Path(get_template("accounts/_hero.html").origin.name).read_text(encoding="utf-8")
        assert "{% blocktranslate count n=hero.this_range.total_merges %}" in src
        assert "changes shipped" in src
        assert '{% translate "shipped since day one" %}' in src
        assert '{% translate "est." %}' in src
        assert "DAIV shipped" in src  # the locked Slack line (D2), blocktranslate asvar
        assert "{% translate 'Copy' %}" in src
        assert '{% translate "No changes shipped yet" %}' in src
        # A range-adaptive label is wrapped too (the ``as`` form).
        assert '{% translate "this week" as range_label %}' in src

    def test_hero_eyebrow_is_range_agnostic_and_mounts_the_partial(self):
        # The reconciled eyebrow can no longer hard-code "Today" and contradict the active range,
        # the live partial is mounted, and the hero section no longer carries the placeholder
        # aria-busy (its content is live now).
        src = Path(get_template("accounts/_console_body.html").origin.name).read_text(encoding="utf-8")
        assert '{% translate "Throughput" %}' in src
        assert '{% include "accounts/_hero.html" %}' in src
        assert 'data-testid="console-hero" aria-labelledby="console-hero-heading" aria-busy="true"' not in src


# ---------------------------------------------------------------------------
# Story 3.2 — Click-through to source (the auditable-in-place breakdown)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestBreakdownReconcilesExactly:
    """AC1/AC5 (NFR1, load-bearing): the revealed rows are exactly the rows the count summed."""

    def test_this_range_rows_equal_headline_count_and_exclude_the_rest(self, member_client, member_user, admin_user):
        # K=3 matched, in-range (now) → counted this range AND all-time.
        for iid in (1, 2, 3):
            _make_merge_metric(iid=iid)
            _make_shipping_run(member_user, iid=iid)
        # matched but out-of-range (40d ago) → excluded this range, present all-time.
        _make_merge_metric(iid=4)
        _make_shipping_run(member_user, iid=4)
        MergeMetric.objects.filter(merge_request_iid=4).update(merged_at=timezone.now() - timedelta(days=40))
        # matched by ANOTHER user's run → excluded from both (personal scope).
        _make_merge_metric(iid=5)
        _make_shipping_run(admin_user, iid=5)
        # human MR, no matching run → excluded from both (attribution join).
        _make_merge_metric(iid=6)

        response = member_client.get(reverse("dashboard"))  # 7d default
        hero = response.context["hero"]
        # Reconcile-exactly by construction: len(this_range_rows) == the headline count.
        assert hero["this_range"]["total_merges"] == 3
        assert len(hero["this_range_rows"]) == 3
        # all-time carries the out-of-range matched row too, still excludes other-user/human.
        assert hero["all_time"] == 4
        assert len(hero["all_time_rows"]) == 4

        range_iids = {row.merge_request_iid for row in hero["this_range_rows"]}
        assert range_iids == {1, 2, 3}
        all_time_iids = {row.merge_request_iid for row in hero["all_time_rows"]}
        assert all_time_iids == {1, 2, 3, 4}
        # Excluded rows appear in neither breakdown.
        assert 5 not in all_time_iids
        assert 6 not in all_time_iids
        # The rendered panel carries one row per underlying merge.
        assert b'data-testid="hero-breakdown"' in response.content
        assert b'data-testid="breakdown-row"' in response.content

    def test_rows_carry_the_row_contract_fields(self, member_client, member_user):
        _make_merge_metric(iid=7, title="Fix the flaky auth test")
        _make_shipping_run(member_user, iid=7, web_url="https://gitlab.example.com/daiv/test/-/merge_requests/7")
        hero = member_client.get(reverse("dashboard")).context["hero"]
        row = hero["this_range_rows"][0]
        assert row.repo_id == "daiv/test"
        assert row.merge_request_iid == 7
        assert row.title == "Fix the flaky auth test"
        assert row.merged_at is not None
        assert row.web_url == "https://gitlab.example.com/daiv/test/-/merge_requests/7"
        assert row.thread_id  # a representative session for the drill-through


@pytest.mark.django_db
class TestBreakdownAdminPersonalScope:
    """AC7: the admin breakdown lists ONLY the admin's own rows — never the by_owner/.all() path."""

    def test_admin_breakdown_is_personal_not_org_wide(self, admin_client, admin_user, member_user):
        _make_merge_metric(iid=1)
        _make_shipping_run(admin_user, iid=1)  # admin's own → listed
        _make_merge_metric(iid=2)
        _make_shipping_run(member_user, iid=2)  # another user's → excluded for admin
        _make_merge_metric(iid=3)  # human MR, no run → excluded

        hero = admin_client.get(reverse("dashboard"), {"period": "all"}).context["hero"]
        assert len(hero["all_time_rows"]) == 1
        assert hero["all_time_rows"][0].merge_request_iid == 1
        assert len(hero["this_range_rows"]) == 1
        assert hero["this_range_rows"][0].merge_request_iid == 1


@pytest.mark.django_db
class TestBreakdownRangeAndOdometer:
    """AC8: headline rows track the active range; odometer rows are range-invariant."""

    def _seed(self, user):
        # two this-week (now) + one 20 days ago; all owned by ``user``.
        for iid in (1, 2, 3):
            _make_merge_metric(iid=iid)
            _make_shipping_run(user, iid=iid)
        MergeMetric.objects.filter(merge_request_iid=3).update(merged_at=timezone.now() - timedelta(days=20))

    def test_headline_rows_match_counted_range(self, member_client, member_user):
        self._seed(member_user)
        seven = member_client.get(reverse("dashboard"), {"period": "7d"}).context["hero"]
        assert len(seven["this_range_rows"]) == seven["this_range"]["total_merges"] == 2
        thirty = member_client.get(reverse("dashboard"), {"period": "30d"}).context["hero"]
        assert len(thirty["this_range_rows"]) == thirty["this_range"]["total_merges"] == 3

    def test_odometer_rows_identical_across_ranges(self, member_client, member_user):
        self._seed(member_user)
        seven = member_client.get(reverse("dashboard"), {"period": "7d"}).context["hero"]
        thirty = member_client.get(reverse("dashboard"), {"period": "30d"}).context["hero"]
        assert len(seven["all_time_rows"]) == len(thirty["all_time_rows"]) == 3

    def test_htmx_fragment_carries_the_rescoped_breakdown(self, member_client, member_user):
        self._seed(member_user)
        response = member_client.get(reverse("dashboard"), {"period": "7d"}, HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        assert b'data-testid="app-sidebar"' not in response.content  # fragment, not the shell
        assert b'data-testid="hero-breakdown"' in response.content
        assert len(response.context["hero"]["this_range_rows"]) == 2

    def test_empty_range_shows_honest_empty_state(self, member_client, member_user):
        # A non-zero all-time but nothing in the active range must not open a blank panel: the
        # breakdown renders an honest "nothing here" row rather than an empty reveal (Edge-A).
        _make_merge_metric(iid=1)
        _make_shipping_run(member_user, iid=1)
        MergeMetric.objects.filter(merge_request_iid=1).update(merged_at=timezone.now() - timedelta(days=20))
        response = member_client.get(reverse("dashboard"), {"period": "7d"})
        ctx = response.context["hero"]
        assert ctx["all_time"] == 1
        assert len(ctx["this_range_rows"]) == 0
        assert b"No changes shipped in this range." in response.content


@pytest.mark.django_db
class TestBreakdownHowComputed:
    """AC2: an honest AD-10 disclosure — no envelope category error, no NON-GOAL promises."""

    def test_how_computed_present_and_honest(self, member_client, member_user):
        _make_merge_metric(iid=1)
        _make_shipping_run(member_user, iid=1)
        # Use the console-body fragment (no shell chrome), so the envelope guard reads the console
        # content only — the shared sidebar renders an unrelated ``{% icon "envelope" %}`` glyph.
        content = member_client.get(reverse("dashboard"), HTTP_HX_REQUEST="true").content
        assert b'data-testid="hero-howcomputed"' in content
        assert b"Changes shipped = merged MRs opened by DAIV" in content
        # The Hero never reads RunEnvelope — the word must not appear in the disclosure (category error).
        assert b"RunEnvelope" not in content
        assert b"envelope" not in content
        # No NON-GOAL metric is promised (diff-survival / clean-vs-edited / sparkline).
        for word in (b"diff-survival", b"sparkline", b"clean-vs", b"dev-hours"):
            assert word not in content


@pytest.mark.django_db
class TestBreakdownMrLinkHonesty:
    """AC11/AC6: link out via the persisted URL only; blank degrades; no live client on render."""

    def test_web_url_links_out_blank_degrades_to_session(self, member_client, member_user):
        _make_merge_metric(iid=1)
        run_linked = _make_shipping_run(
            member_user, iid=1, web_url="https://gitlab.example.com/daiv/test/-/merge_requests/1"
        )
        _make_merge_metric(iid=2)
        run_blank = _make_shipping_run(member_user, iid=2, web_url="")  # blank → no out-link

        content = member_client.get(reverse("dashboard"), {"period": "all"}).content.decode()
        # The set URL renders a real out-link opening in a new tab.
        assert 'href="https://gitlab.example.com/daiv/test/-/merge_requests/1"' in content
        assert 'target="_blank"' in content
        # Never a broken/placeholder link when the URL is blank.
        assert 'href="#"' not in content
        # Both rows still carry a same-tab session drill-through.
        for run in (run_linked, run_blank):
            assert reverse("session_detail", kwargs={"thread_id": run.session_id}) in content

    def test_no_repo_client_instantiated_on_render(self, member_client, member_user):
        _make_merge_metric(iid=1)
        _make_shipping_run(member_user, iid=1, web_url="https://gitlab.example.com/daiv/test/-/merge_requests/1")
        with patch.object(RepoClient, "create_instance") as mock_create_instance:
            member_client.get(reverse("dashboard"))
            member_client.get(reverse("dashboard"), {"period": "all"})
        mock_create_instance.assert_not_called()

    def test_prefers_a_run_with_a_url_over_a_newer_blank_one(self, member_client, member_user):
        # A merge attributed by two of the user's runs: the OLDER persisted the url, a newer re-run
        # left it blank (the field default). The good url must win — a blank newest run must not hide
        # an out-link that genuinely exists (AC11 degrades only when NO attributing run has a url).
        _make_merge_metric(iid=1)
        _make_shipping_run(member_user, iid=1, web_url="https://gitlab.example.com/daiv/test/-/merge_requests/1")
        _make_shipping_run(member_user, iid=1, web_url="")  # newer re-run, blank url
        content = member_client.get(reverse("dashboard"), {"period": "all"}).content.decode()
        assert 'href="https://gitlab.example.com/daiv/test/-/merge_requests/1"' in content


@pytest.mark.django_db
class TestBreakdownA11y:
    """AC10: the trigger is a native <button> with aria-expanded + aria-controls to the panel id."""

    def test_triggers_are_buttons_wired_to_their_panels(self, member_client, member_user):
        _make_merge_metric(iid=1)
        _make_shipping_run(member_user, iid=1)
        content = member_client.get(reverse("dashboard")).content.decode()
        # Both numbers are native buttons (Enter/Space free), not bare text.
        assert '<button type="button" data-testid="hero-headline"' in content
        assert '<button type="button" data-testid="hero-odometer"' in content
        # aria-controls points at a panel id that is actually rendered.
        assert 'aria-controls="hero-breakdown-range"' in content
        assert 'id="hero-breakdown-range"' in content
        assert 'aria-controls="hero-breakdown-alltime"' in content
        assert 'id="hero-breakdown-alltime"' in content
        # aria-expanded reflects state; Esc + click-outside close the panel.
        assert ':aria-expanded="open.toString()"' in content
        assert "@keydown.escape.window" in content
        assert "@click.outside" in content


@pytest.mark.django_db
class TestBreakdownReadOnly:
    """AC6: rendering the breakdown creates/mutates nothing."""

    def test_render_writes_nothing(self, member_client, member_user):
        _make_merge_metric(iid=1)
        _make_shipping_run(member_user, iid=1, web_url="https://gitlab.example.com/daiv/test/-/merge_requests/1")
        before = (MergeMetric.objects.count(), Run.objects.count(), Session.objects.count())
        member_client.get(reverse("dashboard"))
        member_client.get(reverse("dashboard"), {"period": "all"})
        after = (MergeMetric.objects.count(), Run.objects.count(), Session.objects.count())
        assert before == after


@pytest.mark.django_db
class TestFeedDrillThrough:
    """AC3: every Feed item drills through to its session_detail (2.3 wiring — verify)."""

    def test_feed_item_links_to_session_detail(self, member_client, member_user):
        session = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.SCHEDULE, repo_id="daiv/test", user=member_user
        )
        run = Run.objects.create(
            session=session,
            trigger_type=SessionOrigin.SCHEDULE,
            status=RunStatus.SUCCESSFUL,
            repo_id="daiv/test",
            user=member_user,
            finished_at=timezone.now(),
        )
        # An unclassified run renders as "classifying" → the Feed lists it (not the zero-state seal).
        Notification.objects.create(
            recipient=member_user,
            event_type=EventType.RUN_FEED,
            source_type="sessions.Run",
            source_id=str(run.pk),
            subject="nightly",
            body="",
            link_url=reverse("session_detail", kwargs={"thread_id": session.thread_id}),
        )
        content = member_client.get(reverse("dashboard")).content.decode()
        assert reverse("session_detail", kwargs={"thread_id": session.thread_id}) in content


class TestClickThroughI18n:
    """AC4: every new accounts-side click-through string is {% translate %}/{% blocktranslate %}-wrapped."""

    def test_hero_new_strings_are_translated(self):
        src = Path(get_template("accounts/_hero.html").origin.name).read_text(encoding="utf-8")
        assert '{% translate "counted — every merged change" %}' in src
        assert '{% translate "how this is computed" %}' in src

    def test_clickthrough_strings_are_translated(self):
        src = Path(get_template("accounts/_clickthrough.html").origin.name).read_text(encoding="utf-8")
        assert '{% translate "What this counts" %}' in src
        assert '{% translate "view run" %}' in src
        assert "{% blocktranslate %}Changes shipped = merged MRs opened by DAIV" in src
        # The MR out-link aria-label is a blocktranslate carrying the iid placeholder.
        aria_tag = (
            "{% blocktranslate with iid=row.merge_request_iid %}open MR !{{ iid }} on GitLab{% endblocktranslate %}"
        )
        assert aria_tag in src


# ---------------------------------------------------------------------------
# Story 3.3 — reconcile with source of truth (live MR read + freshness stamp)
# ---------------------------------------------------------------------------

_LIVE_READ = "codebase.mr_state.get_merge_request_state"


def _make_scheduled_feed_run(user, *, envelope_status, merge_request_iid=None, repo_id="daiv/test"):
    """A scheduled RUN_FEED run (+ envelope + notification) for ``user``, optionally MR-referencing."""
    session = Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.SCHEDULE, repo_id=repo_id, user=user
    )
    run = Run.objects.create(
        session=session,
        trigger_type=SessionOrigin.SCHEDULE,
        repo_id=repo_id,
        status=RunStatus.SUCCESSFUL,
        user=user,
        merge_request_iid=merge_request_iid,
        finished_at=timezone.now(),
    )
    RunEnvelope.objects.create(run=run, status=envelope_status)
    Notification.objects.create(
        recipient=user,
        event_type=EventType.RUN_FEED,
        source_type="sessions.Run",
        source_id=str(run.pk),
        subject="nightly",
        body="",
        link_url=reverse("session_detail", kwargs={"thread_id": session.thread_id}),
    )
    return run


@pytest.mark.django_db
class TestConsoleReconcile:
    """AC4/AC5/AC8 — the console reflects live MR state and stamps a freshness time."""

    def test_externally_merged_run_leaves_the_feed(self, member_client, member_user):
        from codebase.base import MergeRequestState

        # A lone needs-attention run whose MR merged externally must drop out — the "nothing needs
        # you." seal resolves instead of an awaiting item.
        _make_scheduled_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION, merge_request_iid=7)
        with patch(_LIVE_READ, return_value=MergeRequestState.MERGED):
            response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert response.status_code == 200
        assert 'data-status="needs-attention"' not in content
        assert 'data-testid="feed-zero-state"' in content

    def test_open_mr_run_still_shows_as_awaiting(self, member_client, member_user):
        from codebase.base import MergeRequestState

        _make_scheduled_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION, merge_request_iid=7)
        with patch(_LIVE_READ, return_value=MergeRequestState.OPEN):
            response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert 'data-status="needs-attention"' in content

    def test_read_failure_keeps_the_run_visible(self, member_client, member_user):
        # The wrapper resolves a failed read to OPEN → the item stays (AC6, fail-safe).
        from codebase.base import MergeRequestState

        _make_scheduled_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION, merge_request_iid=7)
        with patch(_LIVE_READ, return_value=MergeRequestState.OPEN):
            response = member_client.get(reverse("dashboard"))
        assert 'data-status="needs-attention"' in response.content.decode()

    def test_all_clear_stays_a_quiet_card_in_a_mixed_feed(self, member_client, member_user):
        # An all-clear run is NOT dropped by reconciliation — only externally-resolved actionable
        # runs leave. A mixed feed keeps the quiet all-clear card.
        from codebase.base import MergeRequestState

        _make_scheduled_feed_run(member_user, envelope_status=EnvelopeStatus.ALL_CLEAR)
        _make_scheduled_feed_run(member_user, envelope_status=EnvelopeStatus.FAILED)
        with patch(_LIVE_READ, return_value=MergeRequestState.OPEN):
            response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert 'data-status="all-clear"' in content
        assert 'data-status="failed"' in content

    def test_reconciled_at_in_context_and_last_checked_rendered(self, member_client, member_user):
        _make_scheduled_feed_run(member_user, envelope_status=EnvelopeStatus.ALL_CLEAR)
        response = member_client.get(reverse("dashboard"))
        assert response.context["reconciled_at"] is not None
        content = response.content.decode()
        assert 'data-testid="feed-reconciled-meta"' in content
        assert "last checked" in content

    def test_last_checked_string_is_translated(self):
        src = Path(get_template("accounts/_feed.html").origin.name).read_text(encoding="utf-8")
        assert '{% blocktranslate with checked=reconciled_at|date:"H:i" %}last checked {{ checked }}' in src

    def test_render_performs_no_writes(self, member_client, member_user):
        from codebase.base import MergeRequestState

        _make_scheduled_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION, merge_request_iid=7)
        before = (MergeMetric.objects.count(), Run.objects.count(), RunEnvelope.objects.count())
        with patch(_LIVE_READ, return_value=MergeRequestState.MERGED):
            member_client.get(reverse("dashboard"))
        after = (MergeMetric.objects.count(), Run.objects.count(), RunEnvelope.objects.count())
        assert before == after

    def test_no_client_instantiated_for_non_mr_feed(self, member_client, member_user):
        # A feed of runs with no MR reference must not reach the live read at all.
        _make_scheduled_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION, merge_request_iid=None)
        with patch(_LIVE_READ) as read:
            response = member_client.get(reverse("dashboard"))
        assert response.status_code == 200
        read.assert_not_called()
