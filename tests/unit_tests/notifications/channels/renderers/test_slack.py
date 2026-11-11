from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from notifications.channels.renderers.base import TONE_EMOJI
from notifications.channels.renderers.slack import COLOR_FAILURE, COLOR_PARTIAL, COLOR_SUCCESS, render
from notifications.choices import EventType


@pytest.fixture(autouse=True)
def _stub_build_absolute_url(monkeypatch):
    # Renderer tests don't need the Sites framework; mock the helper that would hit the DB.
    monkeypatch.setattr(
        "notifications.channels.renderers.base.build_absolute_url", lambda path: f"https://example.com{path}"
    )


def _stub_notification(subject="s", body="b", link_url="/x/", context=None):
    return SimpleNamespace(
        subject=subject,
        body=body,
        link_url=link_url,
        context=context or {},
        created=datetime(2026, 5, 20, 12, 0, tzinfo=UTC),
    )


def _render(event_type, notification):
    return render(SimpleNamespace(**vars(notification), event_type=event_type))


def _fields_by_title(attachment):
    return {f["title"]: f["value"] for f in attachment.get("fields", [])}


class TestRender:
    def test_an_event_without_rows_renders_nothing(self):
        assert _render("does.not.exist", _stub_notification()) is None


class TestToneStyle:
    @pytest.mark.parametrize(
        ("context", "color", "tone"),
        [
            ({"status_tone": "warning"}, COLOR_PARTIAL, "warning"),
            ({"status_tone": "success"}, COLOR_SUCCESS, "success"),
            ({"is_successful": False}, COLOR_FAILURE, "failure"),
        ],
    )
    def test_the_colour_and_emoji_read_the_shared_tone(self, context, color, tone):
        text, [attachment] = _render(EventType.JOB_FINISHED, _stub_notification(context=context))
        assert attachment["color"] == color
        assert text.startswith(f"{TONE_EMOJI[tone]} ")


class TestClassifierReasonRidesEveryAttachment:
    """The summary and lists are attached by ``render`` after the event's rows, so a new event
    cannot ship without the reason a human was paged."""

    @pytest.mark.parametrize(
        "event_type", [EventType.JOB_FINISHED, EventType.SCHEDULE_FINISHED, EventType.JOB_BATCH_FINISHED]
    )
    def test_summary_becomes_the_attachment_text(self, event_type):
        notif = _stub_notification(context={"status_tone": "warning", "summary": "Two flaky auth tests"})
        _text, [attachment] = _render(event_type, notif)
        assert attachment["text"] == "Two flaky auth tests"

    @pytest.mark.parametrize("summary", [None, "", "   "])
    def test_a_blank_summary_omits_the_text_key(self, summary):
        notif = _stub_notification(context={"status_tone": "warning", "summary": summary})
        _text, [attachment] = _render(EventType.JOB_FINISHED, notif)
        assert "text" not in attachment

    def test_both_lists_can_coexist_on_one_attachment(self):
        notif = _stub_notification(
            context={
                "status_tone": "warning",
                "actionable": [{"kind": "bug", "label": "a", "ref": "app/x.py"}],
                "notable_runs": [{"kind": "Failed", "label": "acme/api", "ref": "boom"}],
            }
        )
        _text, [attachment] = _render(EventType.JOB_FINISHED, notif)
        titles = [f["title"] for f in attachment["fields"]]
        assert titles == ["Trigger", "Duration", "Findings", "Needs a look"]


TS = 1779278400


def _attachment(color, title, title_link, fields, **extra):
    return {
        "color": color,
        "title": title,
        "title_link": title_link,
        "fields": fields,
        "footer": "DAIV",
        "ts": TS,
        **extra,
    }


def _short(title, value):
    return {"title": title, "value": value, "short": True}


def _wide(title, value):
    return {"title": title, "value": value, "short": False}


def _assert_same_bytes(actual, expected):
    # httpx serialises the payload in insertion order, so key order is part of the contract.
    assert actual == expected
    assert json.dumps(actual, ensure_ascii=False) == json.dumps(expected, ensure_ascii=False)


class TestPayloadSnapshots:
    """Slack takes the same attachments shape as Rocket Chat, so the pinned payloads
    match that channel's for the same notifications."""

    def test_a_full_context_renders_the_pinned_payload(self):
        notif = _stub_notification(
            subject="Agent run on acme/api needs a look",
            link_url="/sessions/1/",
            context={
                "status_tone": "warning",
                "trigger_label": "Manual",
                "duration_seconds": 84,
                "input_tokens": 12432,
                "output_tokens": 38123,
                "cost_usd": 0.214,
                "summary": "  Two flaky auth tests  ",
                "actionable": [
                    {"kind": "bug", "label": "off-by-one", "ref": "app/views.py:42"},
                    {"kind": "", "label": "flaky", "ref": ""},
                ],
                "actionable_overflow": 3,
            },
        )
        expected = (
            "⚠️ Agent run on acme/api needs a look",
            [
                _attachment(
                    COLOR_PARTIAL,
                    "Agent run on acme/api needs a look",
                    "https://example.com/sessions/1/",
                    [
                        _short("Trigger", "Manual"),
                        _short("Duration", "1m 24s"),
                        _short("Usage", "12.4k in · 38.1k out"),
                        _short("Cost", "$0.21"),
                        _wide("Findings", "• [bug] off-by-one — `app/views.py:42`\n• flaky\n… and 3 more"),
                    ],
                    text="Two flaky auth tests",
                )
            ],
        )
        _assert_same_bytes(_render(EventType.JOB_FINISHED, notif), expected)

    def test_an_empty_context_renders_the_pinned_payload(self):
        rendered = _render(EventType.JOB_FINISHED, _stub_notification(link_url="", context={}))
        expected = ("❌ s", [_attachment(COLOR_FAILURE, "s", "", [_short("Trigger", "—"), _short("Duration", "—")])])
        _assert_same_bytes(rendered, expected)
