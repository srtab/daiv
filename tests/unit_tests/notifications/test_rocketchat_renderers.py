from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from notifications.channels.rocketchat_renderers.base import (
    COLOR_FAILURE,
    COLOR_PARTIAL,
    COLOR_SUCCESS,
    RocketChatRenderer,
)
from notifications.channels.rocketchat_renderers.job_batch_finished import JobBatchFinishedRenderer
from notifications.channels.rocketchat_renderers.job_finished import JobFinishedRenderer
from notifications.channels.rocketchat_renderers.pipeline_watch_exhausted import PipelineWatchExhaustedRenderer
from notifications.channels.rocketchat_renderers.registry import get_renderer
from notifications.channels.rocketchat_renderers.schedule_finished import ScheduleFinishedRenderer
from notifications.choices import EventType


@pytest.fixture(autouse=True)
def _stub_build_absolute_url(monkeypatch):
    # Renderer tests don't need the Sites framework; mock the helper that would hit the DB.
    monkeypatch.setattr(
        "notifications.channels.rocketchat_renderers.base.build_absolute_url", lambda path: f"https://example.com{path}"
    )


def _stub_notification(subject="s", body="b", link_url="/x/", context=None):
    return SimpleNamespace(
        subject=subject,
        body=body,
        link_url=link_url,
        context=context or {},
        created=datetime(2026, 5, 20, 12, 0, tzinfo=UTC),
    )


def _fields_by_title(attachment):
    return {f["title"]: f["value"] for f in attachment.get("fields", [])}


class TestRegistryLookup:
    def test_every_event_type_has_a_renderer(self):
        # A missing renderer is not a hard failure: _build_payload degrades to plain text and logs a
        # warning per delivery. Nothing else stops a new event shipping without a card.
        assert [e.value for e in EventType if get_renderer(e) is None] == []

    def test_the_event_types_map_to_their_own_renderers(self):
        assert isinstance(get_renderer(EventType.JOB_FINISHED), JobFinishedRenderer)
        assert isinstance(get_renderer(EventType.SCHEDULE_FINISHED), ScheduleFinishedRenderer)
        assert isinstance(get_renderer(EventType.JOB_BATCH_FINISHED), JobBatchFinishedRenderer)
        assert isinstance(get_renderer(EventType.PIPELINE_WATCH_EXHAUSTED), PipelineWatchExhaustedRenderer)

    def test_the_package_imports_every_renderer_module(self):
        # Registration is a side effect of importing each submodule, and only __init__ does that in
        # production. Neither the registry nor hasattr() can witness a forgotten entry: this module
        # imports the classes directly, and Python binds a submodule onto its parent package on any
        # import. So read what __init__ actually declares.
        import ast
        import pkgutil
        from pathlib import Path

        from notifications.channels import rocketchat_renderers

        tree = ast.parse(Path(rocketchat_renderers.__file__).read_text())
        declared = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
        modules = {m.name for m in pkgutil.iter_modules(rocketchat_renderers.__path__)} - {"base", "registry"}
        assert modules - declared == set()

    def test_lookup_works_with_bare_string_value(self):
        # Callers receive notification.event_type as a string off the CharField; the registry
        # is keyed by EventType but must resolve via the underlying str (TextChoices is-a str).
        assert isinstance(get_renderer("job.finished"), JobFinishedRenderer)

    def test_unknown_event_type_returns_none(self):
        assert get_renderer("does.not.exist") is None


class TestFormatHelpers:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(None, None), (0, "0"), (42, "42"), (999, "999"), (1000, "1.0k"), (12432, "12.4k"), (596891, "596.9k")],
    )
    def test_fmt_tokens(self, value, expected):
        assert RocketChatRenderer._fmt_tokens(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"), [(None, None), (0, "$0.00"), (0.214321, "$0.21"), (1.5, "$1.50"), (12.345, "$12.35")]
    )
    def test_fmt_cost(self, value, expected):
        assert RocketChatRenderer._fmt_cost(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(None, "—"), (0, "0s"), (47, "47s"), (60, "1m 00s"), (84, "1m 24s"), (3600, "1h 00m"), (3725, "1h 02m")],
    )
    def test_fmt_duration(self, value, expected):
        assert RocketChatRenderer._fmt_duration(value) == expected

    def test_usage_field_returns_none_when_both_tokens_missing(self):
        assert JobFinishedRenderer()._usage_field({"cost_usd": 0.10}) is None

    def test_usage_field_renders_when_only_one_side_present(self):
        field = JobFinishedRenderer()._usage_field({"input_tokens": 1000, "output_tokens": None})
        assert field == {"title": "Usage", "value": "1.0k in · — out", "short": True}

    def test_cost_field_skipped_when_cost_missing(self):
        assert JobFinishedRenderer()._cost_field({"input_tokens": 1000}) is None


class TestJobFinishedRenderer:
    def test_success_renders_green_attachment_with_usage_and_cost(self):
        notif = _stub_notification(
            subject="Agent run on acme/api succeeded",
            link_url="/activities/1/",
            context={
                "is_successful": True,
                "trigger_label": "Manual",
                "duration_seconds": 84,
                "input_tokens": 12432,
                "output_tokens": 38123,
                "cost_usd": 0.214,
            },
        )
        text, attachments = JobFinishedRenderer().render(notif)

        assert text.startswith("✅ ")
        assert "Agent run on acme/api succeeded" in text
        assert len(attachments) == 1
        attachment = attachments[0]
        assert attachment["color"] == COLOR_SUCCESS
        assert attachment["title"] == "Agent run on acme/api succeeded"
        fields = _fields_by_title(attachment)
        assert fields["Trigger"] == "Manual"
        assert fields["Duration"] == "1m 24s"
        assert fields["Usage"] == "12.4k in · 38.1k out"
        assert fields["Cost"] == "$0.21"
        assert attachment["footer"] == "DAIV"

    def test_failure_uses_red_color_and_x_emoji(self):
        notif = _stub_notification(context={"is_successful": False, "trigger_label": "Issue webhook"})
        text, attachments = JobFinishedRenderer().render(notif)
        assert text.startswith("❌ ")
        assert attachments[0]["color"] == COLOR_FAILURE

    def test_warning_tone_renders_amber_and_warning_emoji(self):
        notif = _stub_notification(context={"status_tone": "warning", "trigger_label": "Manual"})
        text, attachments = JobFinishedRenderer().render(notif)
        assert text.startswith("⚠️ ")
        assert attachments[0]["color"] == COLOR_PARTIAL

    def test_status_tone_overrides_is_successful(self):
        # A found-issues run finishes successfully (is_successful True) but its envelope tone is a
        # warning; the attachment must follow the tone, not go green.
        notif = _stub_notification(context={"status_tone": "warning", "is_successful": True})
        _text, attachments = JobFinishedRenderer().render(notif)
        assert attachments[0]["color"] == COLOR_PARTIAL

    def test_usage_and_cost_fields_omitted_when_data_missing(self):
        notif = _stub_notification(context={"is_successful": True, "trigger_label": "Manual"})
        _text, attachments = JobFinishedRenderer().render(notif)
        titles = {f["title"] for f in attachments[0]["fields"]}
        assert "Usage" not in titles
        assert "Cost" not in titles


class TestScheduleFinishedRenderer:
    def test_success_includes_repository_and_owner_fields(self):
        notif = _stub_notification(
            subject="'nightly' succeeded on acme/api — alice",
            context={"is_successful": True, "repo_id": "acme/api", "trigger_owner": "alice", "duration_seconds": 47},
        )
        text, attachments = ScheduleFinishedRenderer().render(notif)
        assert text.startswith("✅ ")
        fields = _fields_by_title(attachments[0])
        assert fields["Repository"] == "acme/api"
        assert fields["Owner"] == "alice"
        assert fields["Duration"] == "47s"

    def test_usage_and_cost_fields_appended_when_present(self):
        # The production signal handler emits these keys for every schedule run, so the
        # append branches in schedule_finished.py need explicit coverage.
        notif = _stub_notification(
            context={
                "is_successful": True,
                "repo_id": "acme/api",
                "trigger_owner": "alice",
                "duration_seconds": 47,
                "input_tokens": 8123,
                "output_tokens": 22456,
                "cost_usd": 0.14,
            }
        )
        _text, attachments = ScheduleFinishedRenderer().render(notif)
        fields = _fields_by_title(attachments[0])
        assert fields["Usage"] == "8.1k in · 22.5k out"
        assert fields["Cost"] == "$0.14"


class TestJobBatchFinishedRenderer:
    def _ctx(self, **overrides):
        base = {
            "found_count": 2,
            "needs_attention_count": 1,
            "failed_count": 1,
            "all_clear_count": 1,
            "notable_count": 4,
            "total": 5,
            "status_tone": "warning",  # the notifier stamps this; the renderer reads it (not the counts)
            "duration_seconds": 371,
            "trigger_owner": "alice",
            "repo_ids": ["acme/api", "acme/web", "acme/cli", "acme/db", "acme/legacy"],
            "input_tokens": 184_217,
            "output_tokens": 412_780,
            "cost_usd": 0.83,
        }
        base.update(overrides)
        return base

    def test_partial_batch_uses_warning_color_and_emoji(self):
        notif = _stub_notification(subject="'nightly' batch: 4/5 need a look — alice", context=self._ctx())
        text, attachments = JobBatchFinishedRenderer().render(notif)
        assert text.startswith("⚠️ ")
        assert attachments[0]["color"] == COLOR_PARTIAL
        fields = _fields_by_title(attachments[0])
        assert fields["Results"] == "⚑ 4 · ✓ 1 of 5"
        assert fields["Breakdown"] == "found 2 · needs 1 · failed 1"
        assert fields["Owner"] == "alice"
        assert fields["Usage"] == "184.2k in · 412.8k out"
        assert fields["Total cost"] == "$0.83"
        assert "acme/api" in fields["Repositories"]

    def test_all_notable_uses_red(self):
        notif = _stub_notification(context=self._ctx(notable_count=5, all_clear_count=0, status_tone="failure"))
        text, attachments = JobBatchFinishedRenderer().render(notif)
        assert text.startswith("❌ ")
        assert attachments[0]["color"] == COLOR_FAILURE

    def test_none_notable_uses_green(self):
        notif = _stub_notification(context=self._ctx(notable_count=0, all_clear_count=5, status_tone="success"))
        text, attachments = JobBatchFinishedRenderer().render(notif)
        assert text.startswith("✅ ")
        assert attachments[0]["color"] == COLOR_SUCCESS

    def test_repo_list_truncates_past_limit(self):
        repo_ids = [f"acme/r{i}" for i in range(12)]
        notif = _stub_notification(context=self._ctx(repo_ids=repo_ids))
        _text, attachments = JobBatchFinishedRenderer().render(notif)
        breakdown = _fields_by_title(attachments[0])["Repositories"]
        # 8-item cap + overflow marker.
        assert "and 4 more" in breakdown
        assert "acme/r11" not in breakdown  # past the cap

    def test_empty_repo_ids_drops_repositories_field(self):
        notif = _stub_notification(context=self._ctx(repo_ids=[]))
        _text, attachments = JobBatchFinishedRenderer().render(notif)
        assert "Repositories" not in _fields_by_title(attachments[0])


class TestClassifierReasonRidesEveryAttachment:
    """The summary and findings are attached in ``_message``, not per renderer, so a new renderer
    cannot ship without the reason a human was paged."""

    @pytest.mark.parametrize(
        "renderer", [JobFinishedRenderer(), ScheduleFinishedRenderer(), JobBatchFinishedRenderer()]
    )
    def test_summary_becomes_the_attachment_text(self, renderer):
        notif = _stub_notification(context={"status_tone": "warning", "summary": "Two flaky auth tests"})
        _text, [attachment] = renderer.render(notif)
        assert attachment["text"] == "Two flaky auth tests"

    @pytest.mark.parametrize("summary", [None, "", "   "])
    def test_a_blank_summary_omits_the_text_key(self, summary):
        # An absent summary must not fall back to the body, which repeats the title.
        notif = _stub_notification(context={"status_tone": "warning", "summary": summary})
        _text, [attachment] = JobFinishedRenderer().render(notif)
        assert "text" not in attachment

    def test_context_without_a_summary_key_still_renders(self):
        _text, [attachment] = JobFinishedRenderer().render(_stub_notification(context={"status_tone": "warning"}))
        assert "text" not in attachment

    def test_findings_render_as_one_long_field(self):
        notif = _stub_notification(
            context={
                "status_tone": "warning",
                "actionable": [
                    {"kind": "bug", "label": "off-by-one in paging", "ref": "app/views.py:42"},
                    {"kind": "test-failure", "label": "auth suite flakes", "ref": "tests/test_auth.py"},
                ],
                "actionable_overflow": 0,
            }
        )
        _text, [attachment] = JobFinishedRenderer().render(notif)
        field = next(f for f in attachment["fields"] if f["title"] == "Findings")
        assert field["short"] is False
        assert field["value"] == (
            "• [bug] off-by-one in paging — `app/views.py:42`\n"
            "• [test-failure] auth suite flakes — `tests/test_auth.py`"
        )

    def test_overflow_gets_its_own_line(self):
        notif = _stub_notification(
            context={
                "status_tone": "warning",
                "actionable": [{"kind": "bug", "label": "a", "ref": "b"}],
                "actionable_overflow": 4,
            }
        )
        _text, [attachment] = JobFinishedRenderer().render(notif)
        assert _fields_by_title(attachment)["Findings"].endswith("… and 4 more")

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
        _text, [attachment] = JobFinishedRenderer().render(notif)
        assert _fields_by_title(attachment)["Findings"] == "• [bug] a — `b`"

    def test_a_finding_missing_kind_or_ref_still_renders_its_label(self):
        notif = _stub_notification(
            context={"status_tone": "warning", "actionable": [{"kind": "", "label": "just a label", "ref": ""}]}
        )
        _text, [attachment] = JobFinishedRenderer().render(notif)
        assert _fields_by_title(attachment)["Findings"] == "• just a label"

    def test_no_findings_adds_no_field(self):
        notif = _stub_notification(context={"status_tone": "failure", "actionable": [], "actionable_overflow": 0})
        _text, [attachment] = JobFinishedRenderer().render(notif)
        assert "Findings" not in _fields_by_title(attachment)

    def test_findings_come_after_the_renderer_own_fields(self):
        notif = _stub_notification(
            context={
                "status_tone": "warning",
                "trigger_label": "Schedule",
                "actionable": [{"kind": "bug", "label": "a", "ref": "b"}],
            }
        )
        _text, [attachment] = JobFinishedRenderer().render(notif)
        assert [f["title"] for f in attachment["fields"]] == ["Trigger", "Duration", "Findings"]


class TestBatchNotableRunsField:
    """A batch's rows put a prose summary in the ``ref`` slot, so they must not be backticked the
    way a finding's file path is."""

    CTX = {
        "status_tone": "warning",
        "notable_runs": [
            {"kind": "Failed", "label": "acme/api", "ref": "migration 0042 errored"},
            {"kind": "Needs attention", "label": "acme/infra", "ref": ""},
        ],
        "notable_runs_overflow": 2,
    }

    def test_notable_runs_render_without_backticks(self):
        _text, [attachment] = JobBatchFinishedRenderer().render(_stub_notification(context=self.CTX))
        assert _fields_by_title(attachment)["Needs a look"] == (
            "• [Failed] acme/api — migration 0042 errored\n• [Needs attention] acme/infra\n… and 2 more"
        )

    def test_findings_keep_their_backticks(self):
        # The same formatter serves both lists; only the batch call opts out of monospace.
        notif = _stub_notification(
            context={"status_tone": "warning", "actionable": [{"kind": "bug", "label": "a", "ref": "app/x.py"}]}
        )
        _text, [attachment] = JobFinishedRenderer().render(notif)
        assert _fields_by_title(attachment)["Findings"] == "• [bug] a — `app/x.py`"

    def test_a_batch_without_notable_rows_adds_no_field(self):
        notif = _stub_notification(context={"status_tone": "success", "notable_runs": []})
        _text, [attachment] = JobBatchFinishedRenderer().render(notif)
        assert "Needs a look" not in _fields_by_title(attachment)

    def test_both_lists_can_coexist_on_one_attachment(self):
        notif = _stub_notification(
            context={
                "status_tone": "warning",
                "actionable": [{"kind": "bug", "label": "a", "ref": "app/x.py"}],
                "notable_runs": [{"kind": "Failed", "label": "acme/api", "ref": "boom"}],
            }
        )
        _text, [attachment] = JobFinishedRenderer().render(notif)
        titles = [f["title"] for f in attachment["fields"]]
        assert titles == ["Trigger", "Duration", "Findings", "Needs a look"]


class TestPipelineWatchExhaustedRenderer:
    @staticmethod
    def _notification():
        return _stub_notification(
            subject="CI still failing on group/repo!7",
            body="I could not get CI green after 3 attempts.",
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
        )

    def test_it_renders_a_failure_card(self):
        _text, attachments = PipelineWatchExhaustedRenderer().render(self._notification())

        assert attachments[0]["color"] == COLOR_FAILURE

    def test_it_reports_the_repo_attempts_and_failing_jobs(self):
        _text, attachments = PipelineWatchExhaustedRenderer().render(self._notification())

        fields = _fields_by_title(attachments[0])
        assert fields["Repository"] == "group/repo"
        assert fields["Attempts"] == "3"
        assert fields["Failing jobs"] == "tests, lint"
        assert fields["Merge request"] == "!7"

    def test_it_carries_the_pipeline_url_unprefixed(self):
        # The CI URL is already absolute; passing it through _link() would prefix the DAIV domain.
        _text, attachments = PipelineWatchExhaustedRenderer().render(self._notification())

        assert _fields_by_title(attachments[0])["Pipeline"] == "https://ci.example.com/p/100"

    def test_the_card_title_still_links_to_the_session(self):
        _text, attachments = PipelineWatchExhaustedRenderer().render(self._notification())

        assert attachments[0]["title_link"] == "https://example.com/sessions/repo-mr-7/"

    def test_a_watch_with_no_named_jobs_omits_the_field(self):
        notification = self._notification()
        notification.context = {**notification.context, "failing_jobs": []}

        _text, attachments = PipelineWatchExhaustedRenderer().render(notification)

        assert "Failing jobs" not in _fields_by_title(attachments[0])
