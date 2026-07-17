"""Markup-level tests for the Review Console shell (Story 2.1).

Mirrors ``test_sidebar.py``: assert on ``response.content`` bytes via ``data-testid``
hooks, reusing the ``admin_client`` / ``member_client`` fixtures from ``conftest.py``.
The console ships as a structural/visual substrate only — no region data, no
job-creation launcher in the console content.
"""

from pathlib import Path

from django.template.loader import get_template
from django.urls import reverse

import pytest

# Repo root: tests/unit_tests/accounts/ -> parents[3].
REPO_ROOT = Path(__file__).resolve().parents[3]
INPUT_CSS = REPO_ROOT / "daiv" / "static_src" / "css" / "input.css"


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
        src = Path(get_template("base_app.html").origin.name).read_text(encoding="utf-8")
        assert '{% translate "All time" %}' in src
        # aria-label wraps the string in single quotes (double-quoted attribute).
        assert 'translate "Time range"' in src or "translate 'Time range'" in src

    @pytest.mark.django_db
    def test_translated_label_renders(self, member_client):
        # Test settings render en (canonical), so the source string appears verbatim.
        response = member_client.get(reverse("dashboard"))
        assert b"Needs-me queue" in response.content
