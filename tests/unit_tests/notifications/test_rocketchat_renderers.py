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
    def test_all_three_event_types_are_registered(self):
        assert isinstance(get_renderer(EventType.JOB_FINISHED), JobFinishedRenderer)
        assert isinstance(get_renderer(EventType.SCHEDULE_FINISHED), ScheduleFinishedRenderer)
        assert isinstance(get_renderer(EventType.JOB_BATCH_FINISHED), JobBatchFinishedRenderer)

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


class TestJobBatchFinishedRenderer:
    def _ctx(self, **overrides):
        base = {
            "successful_count": 4,
            "failed_count": 1,
            "total": 5,
            "duration_seconds": 371,
            "trigger_owner": "alice",
            "repo_results": [
                {"repo": "acme/api", "ok": True},
                {"repo": "acme/web", "ok": True},
                {"repo": "acme/cli", "ok": True},
                {"repo": "acme/db", "ok": True},
                {"repo": "acme/legacy", "ok": False},
            ],
            "input_tokens": 184_217,
            "output_tokens": 412_780,
            "cost_usd": 0.83,
        }
        base.update(overrides)
        return base

    def test_partial_batch_uses_warning_color_and_emoji(self):
        notif = _stub_notification(subject="'nightly' batch: 4/5 succeeded — alice", context=self._ctx())
        text, attachments = JobBatchFinishedRenderer().render(notif)
        assert text.startswith("⚠️ ")
        assert attachments[0]["color"] == COLOR_PARTIAL
        fields = _fields_by_title(attachments[0])
        assert fields["Results"] == "✓ 4 · ✗ 1 of 5"
        assert fields["Owner"] == "alice"
        assert fields["Usage"] == "184.2k in · 412.8k out"
        assert fields["Total cost"] == "$0.83"
        assert "✓ acme/api" in fields["Repositories"]
        assert "✗ acme/legacy" in fields["Repositories"]

    def test_all_succeed_uses_green(self):
        notif = _stub_notification(context=self._ctx(successful_count=5, failed_count=0))
        text, attachments = JobBatchFinishedRenderer().render(notif)
        assert text.startswith("✅ ")
        assert attachments[0]["color"] == COLOR_SUCCESS

    def test_all_fail_uses_red(self):
        notif = _stub_notification(context=self._ctx(successful_count=0, failed_count=5))
        text, attachments = JobBatchFinishedRenderer().render(notif)
        assert text.startswith("❌ ")
        assert attachments[0]["color"] == COLOR_FAILURE

    def test_repo_breakdown_truncates_past_limit(self):
        repo_results = [{"repo": f"acme/r{i}", "ok": True} for i in range(12)]
        notif = _stub_notification(context=self._ctx(repo_results=repo_results))
        _text, attachments = JobBatchFinishedRenderer().render(notif)
        breakdown = _fields_by_title(attachments[0])["Repositories"]
        # 8-item cap + overflow marker.
        assert "and 4 more" in breakdown
        assert "acme/r11" not in breakdown  # past the cap

    def test_empty_repo_results_drops_repositories_field(self):
        notif = _stub_notification(context=self._ctx(repo_results=[]))
        _text, attachments = JobBatchFinishedRenderer().render(notif)
        assert "Repositories" not in _fields_by_title(attachments[0])
