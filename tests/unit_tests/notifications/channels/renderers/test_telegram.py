from __future__ import annotations

import re
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from notifications.channels.renderers.base import REPO_BREAKDOWN_LIMIT
from notifications.channels.renderers.telegram import TG_MAX_CHARS, VIEW_LABEL, render, truncate_escaped
from notifications.choices import EventType


@pytest.fixture(autouse=True)
def _stub_build_absolute_url(monkeypatch):
    monkeypatch.setattr(
        "notifications.channels.renderers.base.build_absolute_url", lambda path: f"https://daiv.test{path}"
    )


def _notif(subject="s", body="b", link_url="/x/", context=None):
    return SimpleNamespace(
        subject=subject,
        body=body,
        link_url=link_url,
        context=context or {},
        created=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
    )


def _render(event_type, notification):
    return render(SimpleNamespace(**vars(notification), event_type=event_type))


class TestRegistry:
    def test_every_event_type_renders_as_html(self):
        # A missing definition falls back to plain text and logs a warning on every delivery.
        assert [e.value for e in EventType if _render(e, _notif()) is None] == []

    def test_lookup_works_with_the_bare_string_off_the_charfield(self):
        assert _render("job.finished", _notif()) is not None

    def test_unknown_event_type_returns_none(self):
        assert _render("does.not.exist", _notif()) is None


class TestTruncateEscaped:
    def test_short_text_is_returned_unchanged(self):
        assert truncate_escaped("hello", 10) == "hello"

    def test_over_budget_text_is_cut_with_an_ellipsis(self):
        assert truncate_escaped("abcdefghij", 5) == "abcd…"

    def test_the_cut_never_lands_inside_an_entity(self):
        # "a&amp;b" cut at 4 would leave a bare "&am" — Telegram answers that with a 400,
        # which the channel files as a PERMANENT failure.
        assert truncate_escaped("a&amp;b", 4) == "a…"

    def test_a_complete_entity_before_the_cut_survives(self):
        assert truncate_escaped("&amp;xyz", 7) == "&amp;x…"

    @pytest.mark.parametrize("limit", [0, -1])
    def test_a_non_positive_budget_renders_empty(self, limit):
        assert truncate_escaped("abc", limit) == ""


class TestEscaping:
    def test_html_metacharacters_in_the_subject_are_escaped(self):
        text, _markup = _render(
            EventType.JOB_FINISHED, _notif(subject="<b>oops</b> & <script>", context={"status_tone": "failure"})
        )
        assert "&lt;b&gt;oops&lt;/b&gt; &amp; &lt;script&gt;" in text
        assert "<script>" not in text

    def test_html_metacharacters_in_a_field_value_are_escaped(self):
        text, _markup = _render(
            EventType.JOB_FINISHED, _notif(context={"status_tone": "warning", "trigger_label": "<img src=x>"})
        )
        assert "&lt;img src=x&gt;" in text

    def test_only_intended_tags_survive(self):
        text, _markup = _render(
            EventType.JOB_FINISHED,
            _notif(subject="a<i>b", context={"status_tone": "success", "trigger_label": "c<u>d"}),
        )
        assert set(re.findall(r"</?([a-z]+)>", text)) == {"b"}


class TestKeyboard:
    def test_a_link_becomes_a_url_button(self):
        _text, markup = _render(
            EventType.JOB_FINISHED, _notif(link_url="/sessions/1/", context={"status_tone": "success"})
        )
        assert markup == {"inline_keyboard": [[{"text": VIEW_LABEL, "url": "https://daiv.test/sessions/1/"}]]}

    def test_no_link_means_no_markup(self):
        # Telegram rejects a URL button with an empty url.
        _text, markup = _render(EventType.JOB_FINISHED, _notif(link_url="", context={"status_tone": "success"}))
        assert markup == {}


class TestJobFinishedRenderer:
    def test_renders_tone_emoji_trigger_duration_usage_and_cost(self):
        text, _markup = _render(
            EventType.JOB_FINISHED,
            _notif(
                subject="Agent run on acme/api needs a look",
                context={
                    "status_tone": "warning",
                    "trigger_label": "Manual",
                    "duration_seconds": 84,
                    "input_tokens": 12432,
                    "output_tokens": 38123,
                    "cost_usd": 0.214,
                },
            ),
        )
        assert text.startswith("⚠️ <b>Agent run on acme/api needs a look</b>")
        assert "1m 24s" in text
        assert "12.4k in · 38.1k out" in text
        assert "$0.21" in text

    def test_missing_usage_and_cost_rows_are_omitted(self):
        text, _markup = _render(
            EventType.JOB_FINISHED, _notif(context={"status_tone": "failure", "trigger_label": "Webhook"})
        )
        assert "Usage" not in text
        assert "Cost" not in text

    def test_status_tone_overrides_is_successful(self):
        text, _markup = _render(
            EventType.JOB_FINISHED, _notif(context={"status_tone": "warning", "is_successful": True})
        )
        assert text.startswith("⚠️ ")

    def test_the_summary_closes_the_message(self):
        text, _markup = _render(
            EventType.JOB_FINISHED,
            _notif(context={"status_tone": "warning", "summary": "Database: Which engine?\nDeadline: When?"}),
        )
        assert text.endswith("<b>Summary:</b> Database: Which engine?\nDeadline: When?")

    def test_the_summary_is_escaped(self):
        text, _markup = _render(
            EventType.JOB_FINISHED, _notif(context={"status_tone": "warning", "summary": "Keep <b> & drop <i>?"})
        )
        assert "Keep &lt;b&gt; &amp; drop &lt;i&gt;?" in text

    def test_a_blank_summary_drops_the_row(self):
        text, _markup = _render(EventType.JOB_FINISHED, _notif(context={"status_tone": "warning", "summary": "  "}))
        assert "Summary" not in text

    def test_a_huge_summary_is_clamped_to_the_bot_api_limit(self):
        text, _markup = _render(
            EventType.JOB_FINISHED, _notif(context={"status_tone": "warning", "summary": "x" * 10_000})
        )
        assert len(text) <= TG_MAX_CHARS
        assert text.endswith("…")


class TestScheduleFinishedRenderer:
    def test_includes_repository_owner_and_duration(self):
        text, _markup = _render(
            EventType.SCHEDULE_FINISHED,
            _notif(
                context={
                    "status_tone": "failure",
                    "repo_id": "acme/api",
                    "trigger_owner": "alice",
                    "duration_seconds": 47,
                }
            ),
        )
        assert text.startswith("❌ ")
        assert "acme/api" in text
        assert "alice" in text
        assert "47s" in text

    def test_the_summary_closes_the_message(self):
        text, _markup = _render(
            EventType.SCHEDULE_FINISHED, _notif(context={"status_tone": "failure", "summary": "migration 0042 errored"})
        )
        assert text.endswith("<b>Summary:</b> migration 0042 errored")


class TestJobBatchFinishedRenderer:
    def _ctx(self, **overrides):
        base = {
            "status_tone": "warning",
            "found_count": 2,
            "needs_attention_count": 1,
            "failed_count": 1,
            "all_clear_count": 1,
            "notable_count": 4,
            "total": 5,
            "duration_seconds": 371,
            "trigger_owner": "alice",
            "repo_ids": ["acme/api", "acme/web"],
            "cost_usd": 0.83,
        }
        base.update(overrides)
        return base

    def test_renders_results_breakdown_and_repositories(self):
        text, _markup = _render(EventType.JOB_BATCH_FINISHED, _notif(context=self._ctx()))
        # assert the rendered fragment, not incidental digits
        assert "⚑ 4 · ✓ 1 of 5" in text
        assert "acme/api" in text and "acme/web" in text

    def test_repo_list_reuses_the_shared_cap(self):
        repos = [f"acme/r{i}" for i in range(REPO_BREAKDOWN_LIMIT + 4)]
        text, _markup = _render(EventType.JOB_BATCH_FINISHED, _notif(context=self._ctx(repo_ids=repos)))
        assert "and 4 more" in text
        assert f"acme/r{REPO_BREAKDOWN_LIMIT}" not in text

    def test_empty_repo_ids_drops_the_row(self):
        text, _markup = _render(EventType.JOB_BATCH_FINISHED, _notif(context=self._ctx(repo_ids=[])))
        assert "Repositories" not in text

    def test_breakdown_names_questions_only_when_there_are_some(self):
        text, _markup = _render(EventType.JOB_BATCH_FINISHED, _notif(context=self._ctx(needs_input_count=2)))
        assert "found 2 · needs 1 · failed 1 · input 2" in text
        text, _markup = _render(EventType.JOB_BATCH_FINISHED, _notif(context=self._ctx()))
        assert "input" not in text


class TestLengthCap:
    def test_a_huge_repo_list_is_clamped_to_the_bot_api_limit(self):
        repos = ["acme/" + "r" * 400 for _ in range(50)]
        text, _markup = _render(
            EventType.JOB_BATCH_FINISHED, _notif(context={"status_tone": "warning", "total": 50, "repo_ids": repos})
        )
        assert len(text) <= TG_MAX_CHARS

    def test_a_huge_subject_is_clamped_and_the_bold_tag_stays_balanced(self):
        text, _markup = _render(
            EventType.JOB_FINISHED,
            _notif(subject="x" * 5000, context={"status_tone": "success", "trigger_label": "Manual"}),
        )
        assert len(text) <= TG_MAX_CHARS
        assert text.count("<b>") == text.count("</b>")

    def test_a_pathological_fixed_row_cannot_blow_the_budget(self):
        # ``repo_id`` is free-form context, not a bounded field: an unbounded fixed row pushes
        # the message past 4096 and Telegram's 400 is filed as permanent, losing the message.
        text, _markup = _render(
            EventType.SCHEDULE_FINISHED,
            _notif(context={"status_tone": "success", "repo_id": "a" * 9000, "trigger_owner": "b" * 9000}),
        )
        assert len(text) <= TG_MAX_CHARS
        assert text.count("<b>") == text.count("</b>")

    def test_a_pathological_fixed_row_is_escaped_before_it_is_cut(self):
        text, _markup = _render(
            EventType.SCHEDULE_FINISHED, _notif(context={"status_tone": "success", "repo_id": "&" * 9000})
        )
        assert len(text) <= TG_MAX_CHARS
        assert re.search(r"&(?![a-z]+;|#\d+;)", text) is None

    def test_the_cut_never_leaves_a_dangling_tag_or_entity(self):
        text, _markup = _render(
            EventType.JOB_BATCH_FINISHED,
            _notif(
                subject="a" * 400, context={"status_tone": "warning", "total": 2, "repo_ids": ["<" * 3000, "&" * 3000]}
            ),
        )
        assert len(text) <= TG_MAX_CHARS
        assert text.count("<b>") == text.count("</b>")
        # No half-written entity: every '&' opens something that closes.
        assert re.search(r"&(?![a-z]+;|#\d+;)", text) is None


def _keyboard(path):
    return {"inline_keyboard": [[{"text": VIEW_LABEL, "url": f"https://daiv.test{path}"}]]}


SNAPSHOTS = {
    EventType.JOB_FINISHED: (
        _notif(
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
                "actionable": [{"kind": "bug", "label": "off-by-one", "ref": "app/views.py:42"}],
                "actionable_overflow": 3,
            },
        ),
        (
            "⚠️ <b>Agent run on acme/api needs a look</b>\n"
            "\n"
            "<b>Trigger:</b> Manual\n"
            "<b>Duration:</b> 1m 24s\n"
            "<b>Usage:</b> 12.4k in · 38.1k out\n"
            "<b>Cost:</b> $0.21\n"
            "<b>Summary:</b> Two flaky auth tests",
            _keyboard("/sessions/1/"),
        ),
    ),
    EventType.SCHEDULE_FINISHED: (
        _notif(
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
        (
            "❌ <b>'nightly' failed on acme/api — alice</b>\n"
            "\n"
            "<b>Repository:</b> acme/api\n"
            "<b>Owner:</b> alice\n"
            "<b>Duration:</b> 1h 02m\n"
            "<b>Usage:</b> 999 in · 1.0k out\n"
            "<b>Cost:</b> $1.50\n"
            "<b>Summary:</b> migration 0042 errored",
            _keyboard("/sessions/2/"),
        ),
    ),
    EventType.JOB_BATCH_FINISHED: (
        _notif(
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
                "notable_runs": [{"kind": "Failed", "label": "acme/api", "ref": "migration errored"}],
                "notable_runs_overflow": 2,
            },
        ),
        (
            "⚠️ <b>'nightly' batch: 4/5 need a look — alice</b>\n"
            "\n"
            "<b>Results:</b> ⚑ 4 · ✓ 1 of 5\n"
            "<b>Breakdown:</b> found 2 · needs 1 · failed 1 · input 1\n"
            "<b>Duration:</b> 6m 11s\n"
            "<b>Owner:</b> alice\n"
            "<b>Usage:</b> 184.2k in · 412.8k out\n"
            "<b>Total cost:</b> $0.83\n"
            "<b>Repositories:</b> acme/r0 · acme/r1 · acme/r2 · acme/r3 · acme/r4 · acme/r5 · acme/r6 · acme/r7"
            " · … and 2 more",
            _keyboard("/sessions/?batch=1"),
        ),
    ),
}

MINIMAL_SNAPSHOTS = {
    EventType.JOB_FINISHED: "❌ <b>s</b>\n\n<b>Trigger:</b> —\n<b>Duration:</b> —",
    EventType.SCHEDULE_FINISHED: "❌ <b>s</b>\n\n<b>Repository:</b> —\n<b>Owner:</b> —\n<b>Duration:</b> —",
    EventType.JOB_BATCH_FINISHED: (
        "❌ <b>s</b>\n\n<b>Results:</b> ⚑ 0 · ✓ 0 of 0\n"
        "<b>Breakdown:</b> found 0 · needs 0 · failed 0\n<b>Duration:</b> —"
    ),
}


class TestPayloadSnapshots:
    @pytest.mark.parametrize("event_type", list(SNAPSHOTS))
    def test_a_full_context_renders_the_pinned_message(self, event_type):
        notification, expected = SNAPSHOTS[event_type]
        assert _render(event_type, notification) == expected

    @pytest.mark.parametrize("event_type", list(MINIMAL_SNAPSHOTS))
    def test_an_empty_context_renders_the_pinned_message(self, event_type):
        rendered = _render(event_type, _notif(link_url="", context={}))
        assert rendered == (MINIMAL_SNAPSHOTS[event_type], {})


def _assert_well_formed(text):
    assert len(text) <= TG_MAX_CHARS
    assert text.count("<b>") == text.count("</b>")
    assert set(re.findall(r"</?([a-z]+)>", text)) == {"b"}
    assert re.search(r"&(?![a-z]+;|#\d+;)", text) is None


class TestPipelineWatchExhaustedRenderer:
    @staticmethod
    def _notification(**context):
        return _notif(
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
                **context,
            },
        )

    def test_renders_the_short_rows_then_the_wide_ones(self):
        assert _render(EventType.PIPELINE_WATCH_EXHAUSTED, self._notification()) == (
            "❌ <b>CI still failing on group/repo!7</b>\n"
            "\n"
            "<b>Repository:</b> group/repo\n"
            "<b>Merge request:</b> !7\n"
            "<b>Attempts:</b> 3\n"
            "<b>Failing jobs:</b> tests, lint\n"
            "<b>Pipeline:</b> https://ci.example.com/p/100",
            _keyboard("/sessions/repo-mr-7/"),
        )

    def test_a_watch_without_a_merge_request_or_named_jobs_omits_those_rows(self):
        text, _markup = _render(
            EventType.PIPELINE_WATCH_EXHAUSTED, self._notification(merge_request_iid=None, failing_jobs=[])
        )
        assert "Merge request" not in text
        assert "Failing jobs" not in text
        assert text.endswith("<b>Pipeline:</b> https://ci.example.com/p/100")

    def test_job_names_and_the_pipeline_url_are_escaped(self):
        text, _markup = _render(
            EventType.PIPELINE_WATCH_EXHAUSTED,
            self._notification(failing_jobs=["build<x>", "a&b"], pipeline_url="https://ci.example.com/p?a=1&b=2"),
        )
        assert "<b>Failing jobs:</b> build&lt;x&gt;, a&amp;b\n" in text
        assert text.endswith("<b>Pipeline:</b> https://ci.example.com/p?a=1&amp;b=2")
        _assert_well_formed(text)

    def test_a_huge_job_list_is_clamped_and_the_rows_after_it_are_dropped(self):
        text, _markup = _render(
            EventType.PIPELINE_WATCH_EXHAUSTED, self._notification(failing_jobs=["<job&" * 50] * 100)
        )
        _assert_well_formed(text)
        assert text.endswith("…")
        assert "Pipeline" not in text

    def test_a_pathological_repository_is_capped_like_any_short_row(self):
        text, _markup = _render(EventType.PIPELINE_WATCH_EXHAUSTED, self._notification(repo_id="&" * 9000))
        _assert_well_formed(text)
        assert text.endswith("<b>Pipeline:</b> https://ci.example.com/p/100")


class TestScheduleDispatchFailedRenderer:
    @staticmethod
    def _notification(**context):
        return _notif(
            subject='Schedule "RT Daily Report" can\'t run',
            link_url="/dashboard/schedules/6/edit/",
            context={
                "status_tone": "failure",
                "status_label": "Can't run",
                "repo_ids": ["sfr/rt-daily-report"],
                "reason": "Owner has no write access to sfr/rt-daily-report",
                "last_run": "Jul 15, 09:30",
                "summary": "Its last scheduled run was skipped.",
                **context,
            },
        )

    def test_renders_the_reason_then_the_summary(self):
        assert _render(EventType.SCHEDULE_DISPATCH_FAILED, self._notification()) == (
            '❌ <b>Schedule "RT Daily Report" can\'t run</b>\n'
            "\n"
            "<b>Repository:</b> sfr/rt-daily-report\n"
            "<b>Last run:</b> Jul 15, 09:30\n"
            "<b>Reason:</b> Owner has no write access to sfr/rt-daily-report\n"
            "<b>Summary:</b> Its last scheduled run was skipped.",
            _keyboard("/dashboard/schedules/6/edit/"),
        )

    def test_several_repos_share_one_plural_row(self):
        text, _markup = _render(EventType.SCHEDULE_DISPATCH_FAILED, self._notification(repo_ids=["a/b", "c/d"]))
        assert "<b>Repositories:</b> a/b · c/d\n" in text
        assert "Repository:" not in text

    def test_the_reason_and_summary_are_escaped(self):
        text, _markup = _render(
            EventType.SCHEDULE_DISPATCH_FAILED,
            self._notification(reason="token <redacted> & expired", summary="Fix <b>it</b> & retry"),
        )
        assert "<b>Reason:</b> token &lt;redacted&gt; &amp; expired\n" in text
        assert text.endswith("<b>Summary:</b> Fix &lt;b&gt;it&lt;/b&gt; &amp; retry")
        _assert_well_formed(text)

    def test_a_huge_summary_is_clamped_after_the_reason(self):
        text, _markup = _render(EventType.SCHEDULE_DISPATCH_FAILED, self._notification(summary="&<" * 5000))
        _assert_well_formed(text)
        assert "<b>Reason:</b> Owner has no write access to sfr/rt-daily-report\n" in text
        assert text.endswith("…")

    def test_a_huge_reason_is_clamped_and_crowds_out_the_summary(self):
        text, _markup = _render(EventType.SCHEDULE_DISPATCH_FAILED, self._notification(reason="<&" * 5000))
        _assert_well_formed(text)
        assert text.endswith("…")
        assert "Summary" not in text
