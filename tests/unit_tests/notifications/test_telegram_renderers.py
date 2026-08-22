from __future__ import annotations

import re
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from notifications.channels.telegram_renderers.base import TG_MAX_CHARS, VIEW_LABEL, TelegramRenderer, truncate_escaped
from notifications.channels.telegram_renderers.job_batch_finished import JobBatchFinishedRenderer
from notifications.channels.telegram_renderers.job_finished import JobFinishedRenderer
from notifications.channels.telegram_renderers.registry import get_renderer
from notifications.channels.telegram_renderers.schedule_finished import ScheduleFinishedRenderer
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


class TestRegistry:
    def test_all_three_event_types_are_registered(self):
        assert isinstance(get_renderer(EventType.JOB_FINISHED), JobFinishedRenderer)
        assert isinstance(get_renderer(EventType.SCHEDULE_FINISHED), ScheduleFinishedRenderer)
        assert isinstance(get_renderer(EventType.JOB_BATCH_FINISHED), JobBatchFinishedRenderer)

    def test_lookup_works_with_the_bare_string_off_the_charfield(self):
        assert isinstance(get_renderer("job.finished"), JobFinishedRenderer)

    def test_unknown_event_type_returns_none(self):
        assert get_renderer("does.not.exist") is None

    def test_the_telegram_registry_is_separate_from_rocket_chats(self):
        from notifications.channels.rocketchat_renderers.registry import get_renderer as rc_get

        assert not isinstance(rc_get(EventType.JOB_FINISHED), JobFinishedRenderer)


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
        text, _markup = JobFinishedRenderer().render(
            _notif(subject="<b>oops</b> & <script>", context={"status_tone": "failure"})
        )
        assert "&lt;b&gt;oops&lt;/b&gt; &amp; &lt;script&gt;" in text
        assert "<script>" not in text

    def test_html_metacharacters_in_a_field_value_are_escaped(self):
        text, _markup = JobFinishedRenderer().render(
            _notif(context={"status_tone": "warning", "trigger_label": "<img src=x>"})
        )
        assert "&lt;img src=x&gt;" in text

    def test_only_intended_tags_survive(self):
        text, _markup = JobFinishedRenderer().render(
            _notif(subject="a<i>b", context={"status_tone": "success", "trigger_label": "c<u>d"})
        )
        assert set(re.findall(r"</?([a-z]+)>", text)) == {"b"}


class TestKeyboard:
    def test_a_link_becomes_a_url_button(self):
        _text, markup = JobFinishedRenderer().render(
            _notif(link_url="/sessions/1/", context={"status_tone": "success"})
        )
        assert markup == {"inline_keyboard": [[{"text": VIEW_LABEL, "url": "https://daiv.test/sessions/1/"}]]}

    def test_no_link_means_no_markup(self):
        # Telegram rejects a URL button with an empty url.
        _text, markup = JobFinishedRenderer().render(_notif(link_url="", context={"status_tone": "success"}))
        assert markup == {}


class TestJobFinishedRenderer:
    def test_renders_tone_emoji_trigger_duration_usage_and_cost(self):
        text, _markup = JobFinishedRenderer().render(
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
            )
        )
        assert text.startswith("⚠️ <b>Agent run on acme/api needs a look</b>")
        assert "1m 24s" in text
        assert "12.4k in · 38.1k out" in text
        assert "$0.21" in text

    def test_missing_usage_and_cost_rows_are_omitted(self):
        text, _markup = JobFinishedRenderer().render(
            _notif(context={"status_tone": "failure", "trigger_label": "Webhook"})
        )
        assert "Usage" not in text
        assert "Cost" not in text

    def test_status_tone_overrides_is_successful(self):
        text, _markup = JobFinishedRenderer().render(_notif(context={"status_tone": "warning", "is_successful": True}))
        assert text.startswith("⚠️ ")


class TestScheduleFinishedRenderer:
    def test_includes_repository_owner_and_duration(self):
        text, _markup = ScheduleFinishedRenderer().render(
            _notif(
                context={
                    "status_tone": "failure",
                    "repo_id": "acme/api",
                    "trigger_owner": "alice",
                    "duration_seconds": 47,
                }
            )
        )
        assert text.startswith("❌ ")
        assert "acme/api" in text
        assert "alice" in text
        assert "47s" in text


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
        text, _markup = JobBatchFinishedRenderer().render(_notif(context=self._ctx()))
        # assert the rendered fragment, not incidental digits
        assert "⚑ 4 · ✓ 1 of 5" in text
        assert "acme/api" in text and "acme/web" in text

    def test_repo_list_reuses_the_shared_cap(self):
        from notifications.channels.renderers.base import REPO_BREAKDOWN_LIMIT

        repos = [f"acme/r{i}" for i in range(REPO_BREAKDOWN_LIMIT + 4)]
        text, _markup = JobBatchFinishedRenderer().render(_notif(context=self._ctx(repo_ids=repos)))
        assert "and 4 more" in text
        assert f"acme/r{REPO_BREAKDOWN_LIMIT}" not in text

    def test_empty_repo_ids_drops_the_row(self):
        text, _markup = JobBatchFinishedRenderer().render(_notif(context=self._ctx(repo_ids=[])))
        assert "Repositories" not in text


class TestLengthCap:
    def test_a_huge_repo_list_is_clamped_to_the_bot_api_limit(self):
        repos = ["acme/" + "r" * 400 for _ in range(50)]
        text, _markup = JobBatchFinishedRenderer().render(
            _notif(context={"status_tone": "warning", "total": 50, "repo_ids": repos})
        )
        assert len(text) <= TG_MAX_CHARS

    def test_a_huge_subject_is_clamped_and_the_bold_tag_stays_balanced(self):
        text, _markup = JobFinishedRenderer().render(
            _notif(subject="x" * 5000, context={"status_tone": "success", "trigger_label": "Manual"})
        )
        assert len(text) <= TG_MAX_CHARS
        assert text.count("<b>") == text.count("</b>")

    def test_the_cut_never_leaves_a_dangling_tag_or_entity(self):
        text, _markup = JobBatchFinishedRenderer().render(
            _notif(
                subject="a" * 400, context={"status_tone": "warning", "total": 2, "repo_ids": ["<" * 3000, "&" * 3000]}
            )
        )
        assert len(text) <= TG_MAX_CHARS
        assert text.count("<b>") == text.count("</b>")
        # No half-written entity: every '&' opens something that closes.
        assert re.search(r"&(?![a-z]+;|#\d+;)", text) is None


class TestEventTypeGuard:
    def test_a_concrete_renderer_without_an_event_type_fails_at_import_time(self):
        with pytest.raises(TypeError, match="must define `event_type`"):

            class Nameless(TelegramRenderer):
                def render(self, notification):
                    return "", {}
