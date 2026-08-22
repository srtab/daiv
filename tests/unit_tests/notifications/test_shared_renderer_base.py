from __future__ import annotations

import pytest
from notifications.channels.renderers.base import REPO_BREAKDOWN_LIMIT, TONE_EMOJI, BaseRenderer
from notifications.channels.rocketchat_renderers.base import (
    COLOR_FAILURE,
    COLOR_PARTIAL,
    COLOR_SUCCESS,
    RocketChatRenderer,
)


class TestToneResolution:
    @pytest.mark.parametrize("tone", ["success", "failure", "warning"])
    def test_the_envelope_tone_is_passed_through(self, tone):
        assert BaseRenderer._tone({"status_tone": tone}) == tone

    def test_a_context_predating_the_tone_key_falls_back_to_is_successful(self):
        assert BaseRenderer._tone({"is_successful": True}) == "success"
        assert BaseRenderer._tone({"is_successful": False}) == "failure"

    def test_an_unknown_tone_falls_back_rather_than_raising(self):
        assert BaseRenderer._tone({"status_tone": "bogus", "is_successful": True}) == "success"

    def test_every_tone_has_an_emoji(self):
        assert set(TONE_EMOJI) == {"success", "failure", "warning"}


class TestSharedFormatHelpers:
    @pytest.mark.parametrize(
        ("value", "expected"), [(None, None), (0, "0"), (999, "999"), (1000, "1.0k"), (12432, "12.4k")]
    )
    def test_fmt_tokens(self, value, expected):
        assert BaseRenderer._fmt_tokens(value) == expected

    @pytest.mark.parametrize(("value", "expected"), [(None, None), (0, "$0.00"), (0.214321, "$0.21")])
    def test_fmt_cost(self, value, expected):
        assert BaseRenderer._fmt_cost(value) == expected

    @pytest.mark.parametrize(("value", "expected"), [(None, "—"), (47, "47s"), (84, "1m 24s"), (3725, "1h 02m")])
    def test_fmt_duration(self, value, expected):
        assert BaseRenderer._fmt_duration(value) == expected


class TestSharedRepoList:
    def test_caps_at_the_shared_limit_with_an_overflow_marker(self):
        repos = [f"acme/r{i}" for i in range(REPO_BREAKDOWN_LIMIT + 4)]
        rendered = BaseRenderer._repo_list(repos)
        assert "and 4 more" in rendered
        assert f"acme/r{REPO_BREAKDOWN_LIMIT}" not in rendered

    def test_empty_input_renders_empty(self):
        assert BaseRenderer._repo_list([]) == ""


class TestRocketChatStillOwnsItsColours:
    def test_tone_style_reads_the_shared_tone_and_emoji(self):
        assert RocketChatRenderer._tone_style({"status_tone": "warning"}) == (COLOR_PARTIAL, TONE_EMOJI["warning"])
        assert RocketChatRenderer._tone_style({"status_tone": "success"}) == (COLOR_SUCCESS, TONE_EMOJI["success"])
        assert RocketChatRenderer._tone_style({"is_successful": False}) == (COLOR_FAILURE, TONE_EMOJI["failure"])

    def test_the_lifted_helpers_still_serve_the_attachment_builders(self):
        # _usage_field / _cost_field referenced RocketChatRenderer._fmt_* by explicit class
        # name; a missed repoint would leave them bound to the subclass they were lifted out of.
        assert RocketChatRenderer._usage_field({"input_tokens": 1000, "output_tokens": None}) == {
            "title": "Usage",
            "value": "1.0k in · — out",
            "short": True,
        }
        assert RocketChatRenderer._cost_field({"cost_usd": 0.14}) == {"title": "Cost", "value": "$0.14", "short": True}

    def test_rocket_chat_renderers_are_still_shared_base_subclasses(self):
        assert issubclass(RocketChatRenderer, BaseRenderer)


class TestEventTypeGuard:
    def test_a_concrete_renderer_without_an_event_type_fails_at_import_time(self):
        with pytest.raises(TypeError, match="must define `event_type`"):

            class Nameless(RocketChatRenderer):
                def render(self, notification):
                    return "", []
