from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from notifications.channels.renderers.base import Row
from notifications.channels.renderers.discord import (
    COLOR_FAILURE,
    COLOR_PARTIAL,
    COLOR_SUCCESS,
    DISCORD_MAX_DESCRIPTION_CHARS,
    DISCORD_MAX_EMBED_CHARS,
    DISCORD_MAX_FIELD_NAME_CHARS,
    DISCORD_MAX_FIELD_VALUE_CHARS,
    DISCORD_MAX_FIELDS,
    DISCORD_MAX_TITLE_CHARS,
    render,
)
from notifications.choices import EventType


@pytest.fixture(autouse=True)
def _stub_build_absolute_url(monkeypatch):
    monkeypatch.setattr(
        "notifications.channels.renderers.base.build_absolute_url", lambda path: f"https://example.com{path}"
    )


def _notification(subject="Subject", body="Body", link_url="/sessions/1/", context=None):
    return SimpleNamespace(
        subject=subject,
        body=body,
        link_url=link_url,
        context=context or {},
        created=datetime(2026, 5, 20, 12, 0, tzinfo=UTC),
    )


def _render(event_type, notification):
    return render(SimpleNamespace(**vars(notification), event_type=event_type))


def test_event_without_rows_renders_nothing():
    assert _render("does.not.exist", _notification()) is None


@pytest.mark.parametrize(
    ("context", "color"),
    [
        ({"status_tone": "warning"}, COLOR_PARTIAL),
        ({"status_tone": "success"}, COLOR_SUCCESS),
        ({"is_successful": False}, COLOR_FAILURE),
    ],
)
def test_tone_becomes_an_integer_colour(context, color):
    embed = _render(EventType.JOB_FINISHED, _notification(context=context))
    assert embed["color"] == color
    assert isinstance(embed["color"], int)


def test_render_builds_one_complete_embed():
    embed = _render(
        EventType.JOB_FINISHED,
        _notification(
            context={
                "status_tone": "warning",
                "trigger_label": "Manual",
                "duration_seconds": 84,
                "summary": "Two flaky auth tests",
                "actionable": [{"kind": "bug", "label": "off-by-one", "ref": "app/views.py:42"}],
                "actionable_overflow": 2,
            }
        ),
    )
    assert embed == {
        "color": COLOR_PARTIAL,
        "title": "Subject",
        "url": "https://example.com/sessions/1/",
        "fields": [
            {"name": "Trigger", "value": "Manual", "inline": True},
            {"name": "Duration", "value": "1m 24s", "inline": True},
            {"name": "Findings", "value": "• [bug] off-by-one — `app/views.py:42`\n… and 2 more", "inline": False},
        ],
        "footer": {"text": "DAIV"},
        "timestamp": "2026-05-20T12:00:00+00:00",
        "description": "Two flaky auth tests",
    }


@pytest.mark.parametrize("summary", [None, "", "   "])
def test_blank_summary_omits_description(summary):
    embed = _render(EventType.JOB_FINISHED, _notification(context={"summary": summary}))
    assert "description" not in embed


def test_embed_field_count_is_capped(monkeypatch):
    rows = [Row(f"Field {index}", "Value") for index in range(30)]
    monkeypatch.setattr("notifications.channels.renderers.discord.event_rows", lambda notification: rows)
    embed = _render(EventType.JOB_FINISHED, _notification())
    assert len(embed["fields"]) == DISCORD_MAX_FIELDS


def test_no_link_omits_the_embed_url():
    embed = _render(EventType.JOB_FINISHED, _notification(link_url=""))
    assert "url" not in embed


def test_embed_limits_are_enforced(monkeypatch):
    rows = [Row("n" * 300, "v" * 1100, wide=index % 2 == 0) for index in range(30)]
    monkeypatch.setattr("notifications.channels.renderers.discord.event_rows", lambda notification: rows)
    embed = _render(
        EventType.JOB_FINISHED,
        _notification(subject="s" * 300, context={"summary": "d" * 4200, "status_tone": "success"}),
    )
    assert len(embed["title"]) == DISCORD_MAX_TITLE_CHARS
    assert len(embed["description"]) == DISCORD_MAX_DESCRIPTION_CHARS
    assert len(embed["fields"]) == 2
    assert len(embed["fields"][0]["name"]) == DISCORD_MAX_FIELD_NAME_CHARS
    assert len(embed["fields"][0]["value"]) == DISCORD_MAX_FIELD_VALUE_CHARS
    assert embed["fields"][0]["name"].endswith("…")
    assert embed["fields"][0]["value"].endswith("…")
    assert embed["fields"][0]["inline"] is False
    assert embed["fields"][1]["inline"] is True
    aggregate = (
        len(embed["title"])
        + len(embed["description"])
        + len(embed["footer"]["text"])
        + sum(len(field["name"]) + len(field["value"]) for field in embed["fields"])
    )
    assert aggregate == DISCORD_MAX_EMBED_CHARS


def test_aggregate_budget_drops_fields_that_do_not_fit(monkeypatch):
    rows = [Row("Reason", "x" * DISCORD_MAX_FIELD_VALUE_CHARS) for _ in range(3)]
    monkeypatch.setattr("notifications.channels.renderers.discord.event_rows", lambda notification: rows)
    embed = _render(
        EventType.JOB_FINISHED,
        _notification(subject="s" * 300, context={"summary": "d" * DISCORD_MAX_DESCRIPTION_CHARS}),
    )
    aggregate = (
        len(embed["title"])
        + len(embed["description"])
        + len(embed["footer"]["text"])
        + sum(len(field["name"]) + len(field["value"]) for field in embed["fields"])
    )
    assert aggregate <= DISCORD_MAX_EMBED_CHARS
    assert len(embed["fields"]) == 2
    assert len(embed["fields"][-1]["value"]) < DISCORD_MAX_FIELD_VALUE_CHARS
