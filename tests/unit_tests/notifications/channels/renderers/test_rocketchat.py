from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from notifications.channels.renderers.base import TONE_EMOJI
from notifications.channels.renderers.rocketchat import COLOR_FAILURE, COLOR_PARTIAL, COLOR_SUCCESS, render
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
        # An absent summary must not fall back to the body, which repeats the title.
        notif = _stub_notification(context={"status_tone": "warning", "summary": summary})
        _text, [attachment] = _render(EventType.JOB_FINISHED, notif)
        assert "text" not in attachment

    def test_a_nonsensical_overflow_count_is_not_rendered(self):
        # The count is read back from persisted context, not recomputed, so the gate is on the sign:
        # a truthiness check would print "… and -2 more" to a recipient.
        notif = _stub_notification(
            context={
                "status_tone": "warning",
                "actionable": [{"kind": "bug", "label": "a", "ref": "b"}],
                "actionable_overflow": -2,
            }
        )
        _text, [attachment] = _render(EventType.JOB_FINISHED, notif)
        assert _fields_by_title(attachment)["Findings"] == "• [bug] a — `b`"

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


SNAPSHOT_CASES = {
    EventType.JOB_FINISHED: _stub_notification(
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
    ),
    EventType.SCHEDULE_FINISHED: _stub_notification(
        subject="'nightly' failed on acme/api — alice",
        link_url="/sessions/2/",
        context={
            "status_tone": "failure",
            "repo_id": "acme/api",
            "trigger_owner": "alice",
            "duration_seconds": 3725,
            "input_tokens": 999,
            "output_tokens": 1000,
            "cost_usd": 1.5,
            "summary": "migration 0042 errored",
        },
    ),
    EventType.JOB_BATCH_FINISHED: _stub_notification(
        subject="'nightly' batch: 4/5 need a look — alice",
        link_url="/sessions/?batch=1",
        context={
            "status_tone": "warning",
            "found_count": 2,
            "needs_attention_count": 1,
            "needs_input_count": 1,
            "failed_count": 1,
            "all_clear_count": 1,
            "notable_count": 4,
            "total": 5,
            "duration_seconds": 371,
            "trigger_owner": "alice",
            "repo_ids": [f"acme/r{i}" for i in range(10)],
            "input_tokens": 184_217,
            "output_tokens": 412_780,
            "cost_usd": 0.83,
            "notable_runs": [
                {"kind": "Failed", "label": "acme/api", "ref": "migration errored"},
                {"kind": "Needs attention", "label": "acme/web", "ref": ""},
            ],
            "notable_runs_overflow": 2,
        },
    ),
    EventType.PIPELINE_WATCH_EXHAUSTED: _stub_notification(
        subject="CI still failing on group/repo!7",
        link_url="/sessions/repo-mr-7/",
        context={
            "status_tone": "failure",
            "status_label": "CI still failing",
            "repo_id": "group/repo",
            "merge_request_iid": 7,
            "attempts": 3,
            "failing_jobs": ["tests", "lint"],
            "pipeline_url": "https://ci.example.com/p/100",
        },
    ),
    EventType.SCHEDULE_DISPATCH_FAILED: _stub_notification(
        subject='Schedule "RT Daily" can\'t run',
        link_url="/dashboard/schedules/6/edit/",
        context={
            "status_tone": "failure",
            "repo_ids": ["a/b", "c/d"],
            "reason": "Owner has no write access",
            "last_run": "Jul 15, 09:30",
            "summary": "Its last scheduled run was skipped.",
        },
    ),
}

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


EXPECTED_FULL = {
    EventType.JOB_FINISHED: (
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
    ),
    EventType.SCHEDULE_FINISHED: (
        "❌ 'nightly' failed on acme/api — alice",
        [
            _attachment(
                COLOR_FAILURE,
                "'nightly' failed on acme/api — alice",
                "https://example.com/sessions/2/",
                [
                    _short("Repository", "acme/api"),
                    _short("Owner", "alice"),
                    _short("Duration", "1h 02m"),
                    _short("Usage", "999 in · 1.0k out"),
                    _short("Cost", "$1.50"),
                ],
                text="migration 0042 errored",
            )
        ],
    ),
    EventType.JOB_BATCH_FINISHED: (
        "⚠️ 'nightly' batch: 4/5 need a look — alice",
        [
            _attachment(
                COLOR_PARTIAL,
                "'nightly' batch: 4/5 need a look — alice",
                "https://example.com/sessions/?batch=1",
                [
                    _short("Results", "⚑ 4 · ✓ 1 of 5"),
                    _short("Breakdown", "found 2 · needs 1 · failed 1 · input 1"),
                    _short("Duration", "6m 11s"),
                    _short("Owner", "alice"),
                    _short("Usage", "184.2k in · 412.8k out"),
                    _short("Total cost", "$0.83"),
                    _wide(
                        "Repositories",
                        "acme/r0 · acme/r1 · acme/r2 · acme/r3 · acme/r4 · acme/r5 · acme/r6 · acme/r7 · … and 2 more",
                    ),
                    _wide(
                        "Needs a look",
                        "• [Failed] acme/api — migration errored\n• [Needs attention] acme/web\n… and 2 more",
                    ),
                ],
            )
        ],
    ),
    EventType.PIPELINE_WATCH_EXHAUSTED: (
        "❌ CI still failing on group/repo!7",
        [
            _attachment(
                COLOR_FAILURE,
                "CI still failing on group/repo!7",
                "https://example.com/sessions/repo-mr-7/",
                [
                    _short("Repository", "group/repo"),
                    _short("Merge request", "!7"),
                    _short("Attempts", "3"),
                    _wide("Failing jobs", "tests, lint"),
                    _wide("Pipeline", "https://ci.example.com/p/100"),
                ],
            )
        ],
    ),
    EventType.SCHEDULE_DISPATCH_FAILED: (
        '❌ Schedule "RT Daily" can\'t run',
        [
            _attachment(
                COLOR_FAILURE,
                'Schedule "RT Daily" can\'t run',
                "https://example.com/dashboard/schedules/6/edit/",
                [
                    _short("Repositories", "a/b · c/d"),
                    _short("Last run", "Jul 15, 09:30"),
                    _wide("Reason", "Owner has no write access"),
                ],
                text="Its last scheduled run was skipped.",
            )
        ],
    ),
}

EXPECTED_MINIMAL_FIELDS = {
    EventType.JOB_FINISHED: [_short("Trigger", "—"), _short("Duration", "—")],
    EventType.SCHEDULE_FINISHED: [_short("Repository", "—"), _short("Owner", "—"), _short("Duration", "—")],
    EventType.JOB_BATCH_FINISHED: [
        _short("Results", "⚑ 0 · ✓ 0 of 0"),
        _short("Breakdown", "found 0 · needs 0 · failed 0"),
        _short("Duration", "—"),
    ],
    EventType.PIPELINE_WATCH_EXHAUSTED: [_short("Repository", "—"), _short("Attempts", "—")],
    EventType.SCHEDULE_DISPATCH_FAILED: [_short("Repository", "—"), _short("Last run", "—"), _wide("Reason", "—")],
}


def _assert_same_bytes(actual, expected):
    # httpx serialises the payload in insertion order, so key order is part of the contract.
    assert actual == expected
    assert json.dumps(actual, ensure_ascii=False) == json.dumps(expected, ensure_ascii=False)


class TestPayloadSnapshots:
    @pytest.mark.parametrize("event_type", list(EXPECTED_FULL))
    def test_a_full_context_renders_the_pinned_payload(self, event_type):
        _assert_same_bytes(_render(event_type, SNAPSHOT_CASES[event_type]), EXPECTED_FULL[event_type])

    @pytest.mark.parametrize("event_type", list(EXPECTED_MINIMAL_FIELDS))
    def test_an_empty_context_renders_the_pinned_payload(self, event_type):
        rendered = _render(event_type, _stub_notification(link_url="", context={}))
        expected = ("❌ s", [_attachment(COLOR_FAILURE, "s", "", EXPECTED_MINIMAL_FIELDS[event_type])])
        _assert_same_bytes(rendered, expected)
