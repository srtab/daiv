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

from django.core.cache import cache
from django.template.loader import get_template, render_to_string
from django.test import Client
from django.urls import reverse
from django.utils import timezone

import pytest
from notifications.choices import EventType
from notifications.models import Notification
from sessions.envelopes import build_actionable_item
from sessions.models import EnvelopeStatus, OfferedAction, Run, RunEnvelope, RunStatus, Session, SessionOrigin

from accounts.models import Role
from accounts.views import get_velocity_data
from codebase.base import MergeRequestState
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
        # Story 4.1: the open-MR shipping run is now a Queue candidate, so the Queue reconciles its
        # live state through the cached ``mr_state`` module. Stub that read so this test isolates the
        # HERO breakdown's own client-free render path (AC6/AC11) — the assertion below then proves the
        # Hero instantiates no client, unclouded by the Queue's (independently-tested) live reconcile.
        with (
            patch(_LIVE_READ, return_value=MergeRequestState.OPEN),
            patch.object(RepoClient, "create_instance") as mock_create_instance,
        ):
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
        # A live MR-state read actually occurs (an actionable MR run) → the render-time "last checked"
        # stamp is honest and rendered alongside the attention list.
        from codebase.base import MergeRequestState

        _make_scheduled_feed_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION, merge_request_iid=7)
        with patch(_LIVE_READ, return_value=MergeRequestState.OPEN):
            response = member_client.get(reverse("dashboard"))
        assert response.context["reconciled_at"] is not None
        content = response.content.decode()
        assert 'data-testid="feed-reconciled-meta"' in content
        assert "last checked" in content

    def test_last_checked_omitted_when_no_live_read_occurs(self, member_client, member_user):
        # Honest freshness (review P2 / NFR1): an all-clear-only feed triggers NO live MR read, so the
        # console must NOT claim a "last checked" source-of-truth verification that never happened.
        _make_scheduled_feed_run(member_user, envelope_status=EnvelopeStatus.ALL_CLEAR)
        response = member_client.get(reverse("dashboard"))
        assert response.context["reconciled_at"] is None
        assert 'data-testid="feed-reconciled-meta"' not in response.content.decode()

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


# ---------------------------------------------------------------------------
# Story 4.1 — The unified Needs-me Queue with a single count
# ---------------------------------------------------------------------------


@pytest.fixture
def _clear_queue_cache():
    """Cold-cache isolation for the Queue tests (Story 4.1 review D9).

    The live MR-state read (``codebase.mr_state.get_merge_request_state``) is backed by the real
    Django cache keyed on ``(repo_id, iid)`` with a 60s TTL. ``@pytest.mark.django_db`` rolls the DB
    back but never clears that cache, so a warm key from a sibling test could otherwise decide a
    cold-cache assertion here. Clearing on both ends removes any iid/order coupling.
    """
    cache.clear()
    yield
    cache.clear()


def _make_queue_run(
    user,
    *,
    status=RunStatus.SUCCESSFUL,
    trigger=SessionOrigin.SCHEDULE,
    envelope_status=None,
    actionable=None,
    merge_request_iid=None,
    merge_request_web_url="",
    title="",
    repo_id="daiv/test",
):
    """Build a Session (owned by ``user``) + a Run + optional RunEnvelope — a Queue candidate.

    Parameterises status / trigger / envelope / MR so the terminal gate, the three candidate
    classes, and the (envelope, status, origin) presentation branches can each be exercised. No
    factory exists; mirrors ``_make_scheduled_feed_run``.
    """
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=trigger, repo_id=repo_id, user=user)
    run = Run.objects.create(
        session=session,
        trigger_type=trigger,
        status=status,
        repo_id=repo_id,
        user=user,
        title=title,
        merge_request_iid=merge_request_iid,
        merge_request_web_url=merge_request_web_url,
        finished_at=timezone.now(),
    )
    if envelope_status is not None:
        RunEnvelope.objects.create(run=run, status=envelope_status, actionable=actionable or [])
    return run


def _found_issues_items():
    """A contract-valid single-item ``actionable[]`` for a FOUND_ISSUES envelope (offered_action=FIX)."""
    return [build_actionable_item(id="f1", kind="finding", label="Null deref in auth", ref="app/auth.py:42")]


@pytest.mark.django_db
@pytest.mark.usefixtures("_clear_queue_cache")
class TestQueueSingleCountAndList:
    """AC1/AC2/AC3: exactly one count pill + one list; queue_count == rendered rows (honest count)."""

    def test_one_count_pill_one_list_count_equals_rows(self, member_client, member_user):
        for _ in range(3):
            _make_queue_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION)
        response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert response.context["queue_count"] == 3
        # One count pill container, no per-type widgets ...
        assert content.count('data-testid="console-queue-count"') == 1
        # ... and the count equals the number of rendered rows (breakdown rows carry a distinct id).
        assert content.count('data-testid="queue-item"') == 3
        assert b'aria-live="polite"' in response.content


@pytest.mark.django_db
@pytest.mark.usefixtures("_clear_queue_cache")
class TestQueueTerminalGate:
    """Review C1 / edge-matrix: an in-flight (non-terminal) run carrying an MR iid is NOT 'needs me'."""

    def test_running_mr_run_excluded_from_queue_and_count(self, member_client, member_user):
        # A genuinely actionable schedule run keeps the queue non-empty ...
        _make_queue_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION)
        # ... and a RUNNING MR-webhook run (already carrying an iid) must be gated out.
        running = _make_queue_run(
            member_user, trigger=SessionOrigin.MR_WEBHOOK, status=RunStatus.RUNNING, merge_request_iid=42
        )
        response = member_client.get(reverse("dashboard"))
        assert response.context["queue_count"] == 1
        assert running.id not in {item["run_id"] for item in response.context["queue_items"]}


@pytest.mark.django_db
@pytest.mark.usefixtures("_clear_queue_cache")
class TestQueueClassifierFlaggedNoMr:
    """Intent-fix regression guard (AMENDED intent-contract): a FOUND_ISSUES-no-MR run is reachable."""

    def test_found_issues_no_mr_appears_counted_and_no_false_seal(self, member_client, member_user):
        run = _make_queue_run(
            member_user,
            trigger=SessionOrigin.SCHEDULE,
            envelope_status=EnvelopeStatus.FOUND_ISSUES,
            actionable=_found_issues_items(),
        )
        # No MR → no live read needed.
        response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert response.context["queue_count"] == 1
        assert 'data-testid="queue-item"' in content
        assert 'data-status="found-issues"' in content
        assert 'data-action="fix"' in content
        # FIX navigates to the run page (the findings live there), never a new-tab MR link.
        assert reverse("session_detail", kwargs={"thread_id": run.session_id}) in content
        # The seal must NOT appear — this is the exact false-"nothing needs you" the widening closes.
        assert 'data-testid="queue-zero-state"' not in content
        assert response.context["queue_zero"] is False


@pytest.mark.django_db
@pytest.mark.usefixtures("_clear_queue_cache")
class TestQueueAntiFlood:
    """Design Notes (three-classes-not-a-flood): a plain done run is NOT flooded in as 'classifying'."""

    def test_plain_successful_non_schedule_run_excluded(self, member_client, member_user):
        # SUCCESSFUL, non-schedule, no envelope, no MR, not FAILED → none of the three classes.
        _make_queue_run(member_user, trigger=SessionOrigin.MCP_JOB, status=RunStatus.SUCCESSFUL)
        response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert response.context["queue_count"] == 0
        assert response.context["queue_zero"] is True
        assert 'data-testid="queue-item"' not in content
        assert 'data-testid="queue-zero-state"' in content
        # The user HAS run(s) → audited-clean, but zero candidates were examined → count clause omitted.
        audit = response.context["queue_audit"]
        assert audit["variant"] == "audited-clean"
        assert audit["checked_count"] == 0


@pytest.mark.django_db
@pytest.mark.usefixtures("_clear_queue_cache")
class TestQueueOriginAware:
    """Review C1/D3: non-schedule open MR → REVIEW (never 'classifying…'); non-retryable → no RETRY."""

    def test_non_schedule_open_mr_renders_review_never_classifying(self, member_client, member_user):
        _make_queue_run(
            member_user,
            trigger=SessionOrigin.MR_WEBHOOK,
            status=RunStatus.SUCCESSFUL,
            merge_request_iid=7,
            merge_request_web_url="https://gitlab.example.com/daiv/test/-/merge_requests/7",
        )
        with patch(_LIVE_READ, return_value=MergeRequestState.OPEN):
            response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert response.context["queue_count"] == 1
        assert 'data-status="needs-attention"' in content
        assert 'data-action="review"' in content
        # NEVER a permanent classifying row for a non-schedule open-MR run.
        assert "classifying" not in content
        # REVIEW of a live MR → the MR link opens in a new tab.
        assert 'href="https://gitlab.example.com/daiv/test/-/merge_requests/7"' in content
        assert 'target="_blank"' in content

    def test_failed_non_retryable_run_offers_no_retry(self, member_client, member_user):
        # A CHAT-origin FAILED run is terminal but NOT retryable (Run.is_retryable is False).
        _make_queue_run(member_user, trigger=SessionOrigin.CHAT, status=RunStatus.FAILED)
        response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert response.context["queue_count"] == 1
        assert 'data-status="failed"' in content
        # No RETRY verb the domain forbids; falls back to a neutral "view run" (NONE).
        assert 'data-action="retry"' not in content
        assert 'data-action="none"' in content

    def test_failed_retryable_run_offers_retry(self, member_client, member_user):
        # An API-job FAILED run IS retryable → RETRY verb.
        _make_queue_run(member_user, trigger=SessionOrigin.API_JOB, status=RunStatus.FAILED)
        response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert response.context["queue_count"] == 1
        assert 'data-action="retry"' in content

    def test_failed_envelope_on_non_retryable_run_downgrades_retry(self, member_client, member_user):
        # Patch 3 (W2): a FAILED envelope maps unconditionally to RETRY. The envelope-present branch
        # (a) must apply the SAME ``is_retryable`` guard branch (b) enforces, so a CHAT-origin FAILED
        # run (not retryable) never advertises a RETRY the domain forbids — it downgrades to NONE.
        _make_queue_run(
            member_user, trigger=SessionOrigin.CHAT, status=RunStatus.FAILED, envelope_status=EnvelopeStatus.FAILED
        )
        response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        # The FAILED envelope keeps the run actionable (is_actionable → RETRY != NONE) so it stays ...
        assert response.context["queue_count"] == 1
        # ... but the rendered verb is downgraded to NONE, never a forbidden RETRY.
        assert response.context["queue_items"][0]["offered_action"] == OfferedAction.NONE
        assert 'data-action="retry"' not in content
        assert 'data-action="none"' in content


@pytest.mark.django_db
@pytest.mark.usefixtures("_clear_queue_cache")
class TestQueueReconcileLiveness:
    """AC5: membership decided by the shared ``still_actionable`` — merged drops, open/failure stays."""

    def test_open_mr_stays(self, member_client, member_user):
        _make_queue_run(member_user, trigger=SessionOrigin.MR_WEBHOOK, status=RunStatus.SUCCESSFUL, merge_request_iid=7)
        with patch(_LIVE_READ, return_value=MergeRequestState.OPEN):
            response = member_client.get(reverse("dashboard"))
        assert response.context["queue_count"] == 1

    def test_externally_merged_mr_drops_and_seals(self, member_client, member_user):
        _make_queue_run(member_user, trigger=SessionOrigin.MR_WEBHOOK, status=RunStatus.SUCCESSFUL, merge_request_iid=7)
        with patch(_LIVE_READ, return_value=MergeRequestState.MERGED):
            response = member_client.get(reverse("dashboard"))
        assert response.context["queue_count"] == 0
        assert 'data-testid="queue-zero-state"' in response.content.decode()

    def test_live_read_failure_keeps_item_visible(self, member_client, member_user, mock_repo_client):
        # A failing provider read is resolved to OPEN by the mr_state fail-safe (NOT patched here), so
        # the item stays visible (AC6 under-claim). Exercises the real wrapper, not a stubbed return.
        mock_repo_client.get_merge_request_state.side_effect = RuntimeError("boom")
        _make_queue_run(member_user, trigger=SessionOrigin.MR_WEBHOOK, status=RunStatus.SUCCESSFUL, merge_request_iid=7)
        response = member_client.get(reverse("dashboard"))
        assert response.context["queue_count"] == 1


@pytest.mark.django_db
@pytest.mark.usefixtures("_clear_queue_cache")
class TestQueueNoFalseSeal:
    """Review D1: reconcile the FULL candidate set — an older actionable item past the newest is kept."""

    def test_older_actionable_not_hidden_behind_a_cap(self, member_client, member_user):
        # 25 NEWER open-MR candidates whose MRs merged externally (reconciled OUT) ...
        for iid in range(1, 26):
            _make_queue_run(
                member_user, trigger=SessionOrigin.MR_WEBHOOK, status=RunStatus.SUCCESSFUL, merge_request_iid=iid
            )
        # ... plus ONE OLDER genuinely-actionable FAILED run (no MR → no live read → stays).
        old = _make_queue_run(member_user, trigger=SessionOrigin.API_JOB, status=RunStatus.FAILED)
        Run.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=10))

        with patch(_LIVE_READ, return_value=MergeRequestState.MERGED):
            response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        # A membership cap would have kept only the 25 newest (all merged → dropped) and sealed the
        # queue with the old actionable item hidden. The full-set reconcile keeps it.
        assert response.context["queue_count"] == 1
        assert old.id in {item["run_id"] for item in response.context["queue_items"]}
        assert 'data-testid="queue-zero-state"' not in content


@pytest.mark.django_db
@pytest.mark.usefixtures("_clear_queue_cache")
class TestQueueHonestSeal:
    """AC9 / NFR1: honest zero-state — never-ran vs audited-clean; never 'N runs all clear'."""

    def test_never_ran_variant_when_no_runs(self, member_client, member_user):
        response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert response.context["queue_zero"] is True
        assert response.context["queue_audit"]["variant"] == "never-ran"
        assert 'data-variant="never-ran"' in content
        assert "nothing needs you." in content

    def test_audited_clean_when_runs_exist_but_none_actionable(self, member_client, member_user):
        # An all-clear scheduled run is not a candidate; a merged open-MR run reconciles out → both
        # leave the queue empty while the user genuinely HAS terminal runs.
        _make_queue_run(member_user, envelope_status=EnvelopeStatus.ALL_CLEAR)
        _make_queue_run(member_user, trigger=SessionOrigin.MR_WEBHOOK, status=RunStatus.SUCCESSFUL, merge_request_iid=3)
        with patch(_LIVE_READ, return_value=MergeRequestState.MERGED):
            response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert response.context["queue_zero"] is True
        assert response.context["queue_audit"]["variant"] == "audited-clean"
        assert 'data-variant="audited-clean"' in content
        # The audit meta must never label the examined runs "all clear" (they include a merged MR).
        assert "all clear" not in content

    def test_only_non_terminal_run_yields_never_ran_not_audited_clean(self, member_client, member_user):
        # Patch 2: the never-ran probe is scoped to TERMINAL runs. A user whose only run is still
        # RUNNING has NOT been checked yet — the seal must be ``never-ran``, not a false
        # ``audited-clean`` ("DAIV checked your work … nothing waiting") over an in-flight run.
        _make_queue_run(member_user, trigger=SessionOrigin.MR_WEBHOOK, status=RunStatus.RUNNING, merge_request_iid=5)
        response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert response.context["queue_zero"] is True
        assert response.context["queue_audit"]["variant"] == "never-ran"
        assert 'data-variant="never-ran"' in content

    def test_last_checked_is_a_stable_real_event_time_not_now(self, member_client, member_user):
        # Patch 1: ``last_checked`` is the real most-recent terminal-run ``finished_at``, NOT
        # ``timezone.now()``. An all-clear scheduled run is terminal but not a Queue candidate → the
        # queue seals audited-clean while the user genuinely has a terminal run to date the check.
        run = _make_queue_run(member_user, envelope_status=EnvelopeStatus.ALL_CLEAR)
        finished = timezone.now() - timedelta(hours=3)
        Run.objects.filter(pk=run.pk).update(finished_at=finished)

        first = member_client.get(reverse("dashboard")).context["queue_audit"]
        second = member_client.get(reverse("dashboard")).context["queue_audit"]

        assert first["variant"] == "audited-clean"
        # Stable across reloads — a ``timezone.now()`` stamp would differ between the two renders ...
        assert first["last_checked"] == second["last_checked"]
        # ... and it is the real terminal finish (~3h ago), never render time.
        assert first["last_checked"] is not None
        assert abs((first["last_checked"] - finished).total_seconds()) < 1
        assert timezone.now() - first["last_checked"] > timedelta(minutes=1)

    def test_pill_recolors_status_clear_at_zero(self, member_client, member_user):
        response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert "you have 0" in content
        # The zero pill uses the status-clear (green) token, not the teal accent.
        assert "--color-status-clear" in content


@pytest.mark.django_db
@pytest.mark.usefixtures("_clear_queue_cache")
class TestQueueAdminPersonalScope:
    """AC8: the admin default Queue is personal — own items only, never by_owner/.all() org leak."""

    def test_admin_sees_only_own_actionable_items(self, admin_client, admin_user, member_user):
        mine = _make_queue_run(admin_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION)
        _make_queue_run(
            member_user, envelope_status=EnvelopeStatus.FOUND_ISSUES, actionable=_found_issues_items()
        )  # another user's — must be excluded
        response = admin_client.get(reverse("dashboard"))
        assert response.context["queue_count"] == 1
        thread_ids = {item["thread_id"] for item in response.context["queue_items"]}
        assert thread_ids == {mine.session_id}

    def test_personal_by_default_still_holds_with_queue(self, admin_client, admin_user):
        _make_queue_run(admin_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION)
        response = admin_client.get(reverse("dashboard"))
        assert "velocity" not in response.context
        assert "total_users" not in response.context
        assert b'data-testid="manager-lens"' not in response.content


@pytest.mark.django_db
@pytest.mark.usefixtures("_clear_queue_cache")
class TestQueueDedupe:
    """Honest-count: a run in more than one class is counted exactly once (single-table OR)."""

    def test_failed_run_with_open_mr_counted_once(self, member_client, member_user):
        # FAILED AND open-MR → matches two OR arms, but a forward-FK/reverse-OneToOne OR cannot fan out.
        _make_queue_run(member_user, trigger=SessionOrigin.API_JOB, status=RunStatus.FAILED, merge_request_iid=5)
        with patch(_LIVE_READ, return_value=MergeRequestState.OPEN):
            response = member_client.get(reverse("dashboard"))
        assert response.context["queue_count"] == 1
        assert response.content.decode().count('data-testid="queue-item"') == 1


@pytest.mark.django_db
@pytest.mark.usefixtures("_clear_queue_cache")
class TestQueueDisclosure:
    """AC6: the count is a <button> wired to the click-through panel, collapsed by default."""

    def test_count_toggle_wired_to_collapsed_panel(self, member_client, member_user):
        _make_queue_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION)
        content = member_client.get(reverse("dashboard")).content.decode()
        assert '<button type="button" data-testid="queue-count-toggle"' in content
        assert 'aria-controls="queue-breakdown"' in content
        assert ':aria-expanded="open.toString()"' in content
        assert 'id="queue-breakdown"' in content
        # Collapsed by default — the reveal is out of the a11y tree until opened.
        assert "x-cloak" in content
        assert "@click.outside" in content


@pytest.mark.django_db
@pytest.mark.usefixtures("_clear_queue_cache")
class TestQueueClickthroughReuse:
    """AC6 reuse: the Hero click-through stays byte-compatible; the Queue gets its own hooks."""

    def test_hero_clickthrough_testids_preserved(self, member_client, member_user):
        _make_merge_metric(iid=1)
        _make_shipping_run(member_user, iid=1)
        content = member_client.get(reverse("dashboard")).content.decode()
        # The default testid_prefix keeps the hero hooks unchanged.
        assert 'data-testid="hero-breakdown"' in content
        assert 'data-testid="hero-howcomputed"' in content

    def test_queue_clickthrough_uses_queue_prefix_and_row(self, member_client, member_user):
        _make_queue_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION)
        content = member_client.get(reverse("dashboard")).content.decode()
        assert 'data-testid="queue-breakdown"' in content
        # how_computed=False → the Queue count has no AD-10 "how computed" disclosure.
        assert 'data-testid="queue-howcomputed"' not in content
        # The Queue supplies its own compact row shape (not the hero merged-MR row).
        assert 'data-testid="queue-breakdown-row"' in content


@pytest.mark.django_db
@pytest.mark.usefixtures("_clear_queue_cache")
class TestQueueHtmxFragment:
    """The Queue region re-renders inside the #console-main HTMX fragment with one count pill."""

    def test_htmx_fragment_has_one_count_pill_and_no_chrome(self, member_client, member_user):
        _make_queue_run(member_user, envelope_status=EnvelopeStatus.NEEDS_ATTENTION)
        response = member_client.get(reverse("dashboard"), HTTP_HX_REQUEST="true")
        content = response.content.decode()
        assert response.status_code == 200
        assert content.count('data-testid="console-queue-count"') == 1
        assert 'data-testid="queue-item"' in content
        # Fragment only — no shell chrome.
        assert 'data-testid="app-sidebar"' not in content
        assert 'data-testid="app-user-menu"' not in content
        assert "<html" not in content


class TestQueueVerbHref:
    """Verb/href agreement (review C5, extended to FIX): the destination must not contradict the verb."""

    def _render(self, *, offered_action, merge_request_web_url="", status_slug="needs-attention"):
        item = {
            "run_id": uuid.uuid4(),
            "repo_id": "daiv/test",
            "title": "boom",
            "merge_request_iid": 9,
            "merge_request_web_url": merge_request_web_url,
            "thread_id": "thread-abc",
            "created_at": timezone.now(),
            "status_slug": status_slug,
            "accent_var": "--color-status-attn",
            "offered_action": offered_action,
            "is_stale": False,
        }
        return render_to_string("accounts/_queue_item.html", {"item": item})

    def test_review_with_url_links_to_mr_new_tab(self):
        url = "https://gitlab.example.com/daiv/test/-/merge_requests/9"
        html = self._render(offered_action=OfferedAction.REVIEW, merge_request_web_url=url)
        assert f'href="{url}"' in html
        assert 'target="_blank"' in html
        assert 'data-action="review"' in html

    def test_review_without_url_links_to_run_page(self):
        html = self._render(offered_action=OfferedAction.REVIEW, merge_request_web_url="")
        assert reverse("session_detail", kwargs={"thread_id": "thread-abc"}) in html
        assert 'target="_blank"' not in html

    def test_fix_links_to_run_page_not_mr(self):
        url = "https://gitlab.example.com/daiv/test/-/merge_requests/9"
        html = self._render(offered_action=OfferedAction.FIX, merge_request_web_url=url, status_slug="found-issues")
        assert reverse("session_detail", kwargs={"thread_id": "thread-abc"}) in html
        # FIX findings live on the run page — never the MR, even with a persisted url.
        assert url not in html

    def test_retry_links_to_run_page_not_mr(self):
        url = "https://gitlab.example.com/daiv/test/-/merge_requests/9"
        html = self._render(offered_action=OfferedAction.RETRY, merge_request_web_url=url, status_slug="failed")
        assert reverse("session_detail", kwargs={"thread_id": "thread-abc"}) in html
        assert url not in html
        assert 'data-action="retry"' in html


class TestQueueClickthroughRowOutLink:
    """Patch 6: the MR out-link aria-label interpolates the iid, so it must never render '!None'."""

    def _render(self, *, merge_request_iid, merge_request_web_url):
        row = {
            "repo_id": "daiv/test",
            "title": "boom",
            "merge_request_iid": merge_request_iid,
            "merge_request_web_url": merge_request_web_url,
            "thread_id": "thread-abc",
            "created_at": timezone.now(),
        }
        return render_to_string("accounts/_queue_clickthrough_row.html", {"row": row})

    def test_url_without_iid_omits_out_link_never_none(self):
        # A web_url present but no iid must not render "open MR !None on GitLab" — the out-link is gated.
        html = self._render(
            merge_request_iid=None, merge_request_web_url="https://gitlab.example.com/daiv/test/-/merge_requests/9"
        )
        assert "!None" not in html
        assert "open MR" not in html  # the whole out-link is omitted

    def test_url_with_iid_still_renders_out_link(self):
        # The healthy path is unchanged: url + iid → a real out-link with the iid in its aria-label.
        url = "https://gitlab.example.com/daiv/test/-/merge_requests/9"
        html = self._render(merge_request_iid=9, merge_request_web_url=url)
        assert f'href="{url}"' in html
        assert "open MR !9 on GitLab" in html
        assert "!None" not in html


@pytest.mark.django_db
@pytest.mark.usefixtures("_clear_queue_cache")
class TestQueueDeterministicOrdering:
    """Same-age candidates keep the ``("-created_at", "-id")`` tiebreak under 4.2's impact sort."""

    def test_same_age_rows_are_deterministic(self, member_client, member_user):
        # Five FAILED API-job candidates sharing one age (created_at AND finished_at). All are the
        # same impact class (passive-decay) with an identical age key, so ``order_queue``'s stable
        # sort leaves them in Story 4.1's incoming ``("-created_at", "-id")`` order — deterministic
        # across HTMX re-renders, never reshuffled by the impact re-sequence.
        runs = [_make_queue_run(member_user, trigger=SessionOrigin.API_JOB, status=RunStatus.FAILED) for _ in range(5)]
        pinned = timezone.now()
        Run.objects.filter(pk__in=[r.pk for r in runs]).update(created_at=pinned, finished_at=pinned)

        first = [item["run_id"] for item in member_client.get(reverse("dashboard")).context["queue_items"]]
        second = [item["run_id"] for item in member_client.get(reverse("dashboard")).context["queue_items"]]

        assert len(first) == 5
        assert first == second  # stable across re-renders
        expected = list(
            Run.objects.filter(pk__in=[r.pk for r in runs]).order_by("-created_at", "-id").values_list("id", flat=True)
        )
        assert first == expected


@pytest.mark.django_db
@pytest.mark.usefixtures("_clear_queue_cache")
class TestQueueImpactOrdering:
    """Story 4.2 — impact-based ordering. v1 (every item passive-decay) sorts most-stale first; the
    re-sequence never changes membership or the count, and priority is position + chip only."""

    @staticmethod
    def _age(run, when):
        # Age off BOTH timestamps so ``finished_at or created_at`` (the 4.2 clock) is unambiguous.
        Run.objects.filter(pk=run.pk).update(created_at=when, finished_at=when)

    def _failed(self, user, repo_id):
        return _make_queue_run(user, trigger=SessionOrigin.API_JOB, status=RunStatus.FAILED, repo_id=repo_id)

    def test_most_stale_first_inverts_newest_first(self, member_client, member_user):
        # Three actionable failed runs of distinct ages. v1 orders them OLDEST-first — the observable
        # inversion of Story 4.1's newest-first placeholder (AC1/AC4). A failure never ranks by
        # loudness; within passive-decay, age decides.
        now = timezone.now()
        newest, middle, oldest = (self._failed(member_user, f"daiv/{n}") for n in ("newest", "middle", "oldest"))
        self._age(newest, now - timedelta(days=1))
        self._age(middle, now - timedelta(days=10))
        self._age(oldest, now - timedelta(days=40))

        items = member_client.get(reverse("dashboard")).context["queue_items"]
        assert [it["run_id"] for it in items] == [oldest.pk, middle.pk, newest.pk]

    def test_ordering_is_a_pure_resequence(self, member_client, member_user):
        # AC6/AC7: ordering changes only the sequence — the SET of items and the honest count are
        # unchanged, and ``queue_count`` still equals the rendered rows.
        now = timezone.now()
        runs = [self._failed(member_user, f"daiv/r{i}") for i in range(4)]
        for i, run in enumerate(runs):
            self._age(run, now - timedelta(days=i))

        ctx = member_client.get(reverse("dashboard")).context
        assert ctx["queue_count"] == 4
        assert ctx["queue_count"] == len(ctx["queue_items"])  # honest count (AC7)
        assert {it["run_id"] for it in ctx["queue_items"]} == {r.pk for r in runs}  # membership intact (AC6)

    def test_dom_order_equals_context_order(self, member_client, member_user):
        # AC10: rows render server-side in the ordered sequence, so DOM order == visual order ==
        # context order (tab order == reading order). No CSS ``order`` reflow desyncs the two.
        now = timezone.now()
        for i in range(3):
            self._age(self._failed(member_user, f"daiv/ord{i}"), now - timedelta(days=(3 - i)))

        resp = member_client.get(reverse("dashboard"))
        content = resp.content.decode()
        ordered_repos = [it["repo_id"] for it in resp.context["queue_items"]]
        dom_positions = [content.index(repo) for repo in ordered_repos]
        assert dom_positions == sorted(dom_positions)  # the DOM follows the context order exactly
        assert ordered_repos == ["daiv/ord0", "daiv/ord1", "daiv/ord2"]  # oldest-first

    def test_no_rank_number_and_stripe_stays_per_status(self, member_client, member_user):
        # AC9: priority is position + chip only — no ordinal rank number/badge; the left stripe stays
        # per-STATUS (``--color-status-*``), never repurposed as an impact-order hue.
        self._failed(member_user, "daiv/failed")
        content = member_client.get(reverse("dashboard")).content.decode()
        assert "border-left-color: var(--color-status-fail)" in content  # per-status stripe intact
        assert 'data-testid="queue-item-rank"' not in content  # no rank badge/number rendered


@pytest.mark.django_db
@pytest.mark.usefixtures("_clear_queue_cache")
class TestQueueStaleTag:
    """Passive-decay indication: a row older than QUEUE_DECAY_STALE_AFTER surfaces a ``stale · Nd`` chip."""

    def test_old_actionable_run_is_tagged_stale(self, member_client, member_user):
        old = _make_queue_run(member_user, trigger=SessionOrigin.API_JOB, status=RunStatus.FAILED)
        thirty_days_ago = timezone.now() - timedelta(days=30)
        # Age off ``finished_at or created_at`` (the 4.2 clock, AC4): move BOTH so the item is stale.
        Run.objects.filter(pk=old.pk).update(created_at=thirty_days_ago, finished_at=thirty_days_ago)
        response = member_client.get(reverse("dashboard"))
        item = response.context["queue_items"][0]
        assert item["is_stale"] is True
        assert item["stale_days"] == 30
        # The chip is enriched with the age (AC4) — no longer a bare "stale".
        assert "stale · 30d" in response.content.decode()
        assert 'data-testid="queue-item-stale"' in response.content.decode()

    def test_recent_run_is_not_stale(self, member_client, member_user):
        _make_queue_run(member_user, trigger=SessionOrigin.API_JOB, status=RunStatus.FAILED)
        response = member_client.get(reverse("dashboard"))
        assert response.context["queue_items"][0]["is_stale"] is False
        assert 'data-testid="queue-item-stale"' not in response.content.decode()


@pytest.mark.django_db
@pytest.mark.usefixtures("_clear_queue_cache")
class TestQueueReadOnly:
    """Presentation-only: rendering the Queue creates/mutates nothing (no model, no migration path)."""

    def test_render_writes_nothing(self, member_client, member_user):
        _make_queue_run(member_user, trigger=SessionOrigin.MR_WEBHOOK, status=RunStatus.SUCCESSFUL, merge_request_iid=7)
        before = (Run.objects.count(), RunEnvelope.objects.count(), Session.objects.count())
        with patch(_LIVE_READ, return_value=MergeRequestState.OPEN):
            member_client.get(reverse("dashboard"))
        after = (Run.objects.count(), RunEnvelope.objects.count(), Session.objects.count())
        assert before == after


class TestQueueI18n:
    """AC7: every new Queue string is {% translate %}/{% blocktranslate %}-wrapped (source read)."""

    def test_count_pill_is_translated(self):
        src = Path(get_template("accounts/_console_body.html").origin.name).read_text(encoding="utf-8")
        assert "{% blocktranslate count n=queue_count %}you have {{ n }}" in src

    def test_queue_item_verbs_are_translated(self):
        src = Path(get_template("accounts/_queue_item.html").origin.name).read_text(encoding="utf-8")
        assert '{% translate "Review" %}' in src
        assert '{% translate "Fix" %}' in src
        assert '{% translate "Retry" %}' in src
        assert '{% translate "View run" %}' in src
        assert '{% translate "needs attention" %}' in src

    def test_stale_impact_chip_is_translated(self):
        # Story 4.2 (AC11): the new passive-decay chip is a blocktranslate with a ``days`` var,
        # English as source. ``pt`` is produced via makemessages, never hand-authored here.
        src = Path(get_template("accounts/_queue_item.html").origin.name).read_text(encoding="utf-8")
        assert "{% blocktranslate with days=item.stale_days %}stale · {{ days }}d{% endblocktranslate %}" in src

    def test_queue_zero_state_is_translated(self):
        src = Path(get_template("accounts/_queue_zero_state.html").origin.name).read_text(encoding="utf-8")
        assert '{% translate "nothing needs you." %}' in src
        assert '{% translate "No runs yet." %}' in src


# ---------------------------------------------------------------------------
# Story 5.1 — Finding -> Fix affordance (render presence/absence on both surfaces)
# ---------------------------------------------------------------------------

# The exact inline amber every ``fix it`` control reuses (Feed + Queue stay byte-identical). Asserting
# the literal string guards the "no new Tailwind class / reuse the proven amber" styling contract.
_FIX_AMBER = (
    "color: var(--color-status-found); "
    "background-color: color-mix(in srgb, var(--color-status-found) 12%, transparent); "
    "border: 1px solid color-mix(in srgb, var(--color-status-found) 28%, transparent)"
)
_FIX_PROMPT = "Repair the null dereference in the auth guard."


def _fixable_items():
    """A contract-valid single-item ``actionable[]`` carrying a non-empty ``fix_prompt`` (offers FIX)."""
    return [
        build_actionable_item(
            id="f1", kind="finding", label="Null deref in auth", ref="app/auth.py:42", fix_prompt=_FIX_PROMPT
        )
    ]


def _make_fixable_feed_run(user, *, repo_id="daiv/test"):
    """A found-issues RUN_FEED run whose actionable item carries a ``fix_prompt`` (fix-able)."""
    session = Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.SCHEDULE, repo_id=repo_id, user=user
    )
    run = Run.objects.create(
        session=session,
        trigger_type=SessionOrigin.SCHEDULE,
        repo_id=repo_id,
        status=RunStatus.SUCCESSFUL,
        user=user,
        finished_at=timezone.now(),
    )
    RunEnvelope.objects.create(run=run, status=EnvelopeStatus.FOUND_ISSUES, actionable=_fixable_items())
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
@pytest.mark.usefixtures("_clear_queue_cache")
class TestQueueFixAffordance:
    """AC1/AC6/AC8 — a fix-able FOUND_ISSUES Queue row offers the preview trigger; no-prompt does not."""

    def _queue(self, member_client):
        content = member_client.get(reverse("dashboard")).content.decode()
        return content.split('data-testid="console-queue"')[1].split('data-testid="console-feed"')[0]

    def test_fixable_row_offers_amber_preview_trigger(self, member_client, member_user):
        run = _make_queue_run(
            member_user,
            trigger=SessionOrigin.SCHEDULE,
            envelope_status=EnvelopeStatus.FOUND_ISSUES,
            actionable=_fixable_items(),
        )
        queue = self._queue(member_client)
        # A preview trigger (hx-get into the persistent mount), never a direct launch/POST from the row.
        assert reverse("feed_item_fix", kwargs={"run_id": run.id}) in queue
        assert "?surface=queue" in queue
        assert 'data-action="fix"' in queue
        assert 'hx-target="#fix-preview-mount"' in queue
        assert _FIX_AMBER in queue
        # It is the preview trigger, not the navigate-to-run link.
        assert 'hx-get="' in queue.split('data-testid="queue-item-action"')[1][:400]

    def test_found_issues_without_fix_prompt_keeps_navigate_link(self, member_client, member_user):
        # FOUND_ISSUES with actionable items but NO fix_prompt → offered_action is still FIX, yet NO
        # launch affordance: the row navigates to the run page and never opens the preview (AC8).
        run = _make_queue_run(
            member_user,
            trigger=SessionOrigin.SCHEDULE,
            envelope_status=EnvelopeStatus.FOUND_ISSUES,
            actionable=_found_issues_items(),
        )
        queue = self._queue(member_client)
        assert reverse("feed_item_fix", kwargs={"run_id": run.id}) not in queue
        assert reverse("session_detail", kwargs={"thread_id": run.session_id}) in queue


@pytest.mark.django_db
@pytest.mark.usefixtures("_clear_queue_cache")
class TestFeedFixAffordance:
    """AC1/AC8 — a fix-able found-issues Feed item offers the preview trigger; no-prompt does not."""

    def _feed(self, member_client):
        return member_client.get(reverse("dashboard")).content.decode().split('data-testid="console-feed"')[1]

    def test_fixable_feed_item_offers_amber_preview_trigger(self, member_client, member_user):
        run = _make_fixable_feed_run(member_user)
        feed = self._feed(member_client)
        assert reverse("feed_item_fix", kwargs={"run_id": run.id}) in feed
        assert "?surface=feed" in feed
        assert 'data-testid="feed-item-action"' in feed
        assert 'data-action="fix"' in feed
        assert 'hx-target="#fix-preview-mount"' in feed
        assert _FIX_AMBER in feed

    def test_found_issues_feed_item_without_fix_prompt_offers_nothing(self, member_client, member_user):
        # A found-issues feed item whose actionable items carry no fix_prompt shows no fix affordance.
        session = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.SCHEDULE, repo_id="daiv/test", user=member_user
        )
        run = Run.objects.create(
            session=session,
            trigger_type=SessionOrigin.SCHEDULE,
            repo_id="daiv/test",
            status=RunStatus.SUCCESSFUL,
            user=member_user,
            finished_at=timezone.now(),
        )
        RunEnvelope.objects.create(run=run, status=EnvelopeStatus.FOUND_ISSUES, actionable=_found_issues_items())
        Notification.objects.create(
            recipient=member_user,
            event_type=EventType.RUN_FEED,
            source_type="sessions.Run",
            source_id=str(run.pk),
            subject="n",
            body="",
            link_url="/",
        )
        feed = self._feed(member_client)
        assert 'data-testid="feed-item-action"' not in feed
        assert reverse("feed_item_fix", kwargs={"run_id": run.id}) not in feed


@pytest.mark.django_db
@pytest.mark.usefixtures("_clear_queue_cache")
class TestFixAffordanceIdenticalAcrossSurfaces:
    """AC1 — the Feed and Queue ``fix it`` reuse the identical amber treatment + verb."""

    def test_both_surfaces_render_identical_amber_fix_it(self, member_client, member_user):
        _make_queue_run(
            member_user,
            trigger=SessionOrigin.SCHEDULE,
            envelope_status=EnvelopeStatus.FOUND_ISSUES,
            actionable=_fixable_items(),
        )
        _make_fixable_feed_run(member_user, repo_id="daiv/other")
        content = member_client.get(reverse("dashboard")).content.decode()
        # The exact amber appears on both surfaces (queue row + feed item action).
        assert content.count(_FIX_AMBER) >= 2
        # Both carry the reused OfferedAction.FIX verb ("Fix it") on a preview trigger.
        assert content.count('hx-target="#fix-preview-mount"') >= 2


class TestFixPreviewI18nAndSafety:
    """AC6/AC10 — new preview strings are externalized; fix_prompt is inert (autoescaped, never |safe)."""

    def test_preview_strings_are_translated(self):
        src = Path(get_template("accounts/_fix_preview.html").origin.name).read_text(encoding="utf-8")
        assert '{% translate "Start a fix" %}' in src
        assert '{% translate "Fix it" %}' in src
        assert '{% translate "Cancel" %}' in src
        assert '{% translate "What will be attempted" %}' in src

    def test_fix_prompt_is_inert_never_safe(self):
        src = Path(get_template("accounts/_fix_preview.html").origin.name).read_text(encoding="utf-8")
        # Rendered via plain autoescaping — never marked |safe, never as a template with user context.
        assert "{{ fix_prompt }}" in src
        assert "fix_prompt|safe" not in src
        assert "fix_prompt |safe" not in src

    def test_started_and_notice_strings_are_translated(self):
        started = Path(get_template("accounts/_fix_started.html").origin.name).read_text(encoding="utf-8")
        assert '{% translate "Fix started" %}' in started
        # The queue item keeps its navigate-only "Fix" label alongside the new "Fix it" preview verb.
        queue = Path(get_template("accounts/_queue_item.html").origin.name).read_text(encoding="utf-8")
        assert '{% translate "Fix it" %}' in queue
        assert '{% translate "Fix" %}' in queue
