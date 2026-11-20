from datetime import timedelta

from django.template import Context, Template
from django.utils import timezone
from django.utils.formats import date_format

import pytest
from dashboard.templatetags.nav_tags import compact_time


def _render(template_source: str, context_dict: dict) -> str:
    return Template(template_source).render(Context(context_dict))


class TestNavActive:
    def test_returns_active_classes_when_section_matches(self):
        ctx = {"nav_active_section": "activity"}
        out = _render("{% load nav_tags %}{% nav_active 'activity' %}", ctx)
        assert "bg-surface-3" in out
        assert "text-text-strong" in out
        # Draws the 3px brand rail; every inactive item then carries no indicator at all.
        assert "sidebar__nav-item--active" in out

    def test_returns_empty_string_when_section_does_not_match(self):
        ctx = {"nav_active_section": "activity"}
        out = _render("{% load nav_tags %}{% nav_active 'schedules' %}", ctx)
        assert out.strip() == ""

    def test_returns_empty_string_when_no_active_section_in_context(self):
        out = _render("{% load nav_tags %}{% nav_active 'activity' %}", {})
        assert out.strip() == ""


class TestCompactTime:
    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (timedelta(seconds=-20), "now"),
            (timedelta(minutes=-5, seconds=-10), "5m"),
            (timedelta(hours=-3, minutes=-10), "3h"),
            (timedelta(days=-2, hours=-1), "2d"),
            (timedelta(seconds=20), "now"),
            (timedelta(minutes=20, seconds=30), "in 20m"),
            (timedelta(hours=3, minutes=30), "in 3h"),
            (timedelta(days=1, hours=2), "in 1d"),
        ],
    )
    def test_short_relative_time(self, delta, expected):
        assert compact_time(timezone.now() + delta) == expected

    @pytest.mark.parametrize("days", [-10, 10])
    def test_a_week_or_more_away_shows_the_date(self, days):
        moment = timezone.now() + timedelta(days=days)
        assert compact_time(moment) == date_format(timezone.localtime(moment), "M j")
