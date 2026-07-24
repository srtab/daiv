"""Markup-level tests for the Review Console shell (Story 2.1).

Mirrors ``test_sidebar.py``: assert on ``response.content`` bytes via ``data-testid``
hooks, reusing the ``admin_client`` / ``member_client`` fixtures from ``conftest.py``.
The console ships as a structural/visual substrate only — no region data, no
job-creation launcher in the console content.
"""

from datetime import timedelta
from pathlib import Path

from django.template.loader import get_template
from django.test import Client
from django.urls import reverse
from django.utils import timezone

import pytest

from accounts.models import Role
from codebase.models import MergeMetric, PlatformType

# Repo root: tests/unit_tests/accounts/ -> parents[3].
REPO_ROOT = Path(__file__).resolve().parents[3]
INPUT_CSS = REPO_ROOT / "daiv" / "static_src" / "css" / "input.css"


def _make_merge_metric(*, iid=1, daiv_commits=1, total_commits=2, lines_added=10, lines_removed=3):
    """Create a ``MergeMetric`` row directly (no factory exists).

    Fills the required non-default fields (``merged_at``/``target_branch``/``source_branch``/
    ``platform``); ``merged_at`` is *now* so the row survives the default "today" period filter.
    Distinct ``iid`` values keep the ``(repo_id, merge_request_iid, platform)`` uniqueness happy.
    """
    return MergeMetric.objects.create(
        repo_id="daiv/test",
        merge_request_iid=iid,
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
