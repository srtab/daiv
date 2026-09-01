"""The Telegram bot token must not reach Sentry through a URL.

``sentry_sdk.utils.parse_url(url, sanitize=True)`` strips userinfo and query values but leaves
the *path* intact, and the Bot API takes the token in the path — so the httpx integration would
record a full-control credential in every breadcrumb and span. These cover the scrubbing
function itself; booting the SDK is out of scope.
"""

from __future__ import annotations

import importlib
import os
from unittest import mock

import pytest

TOKEN_URL = "https://api.telegram.org/bot8123456789:AAH-real-looking-token_x/sendMessage"  # noqa: S105 — test constant


def _component():
    """Import the settings component with no DSN, so importing it cannot initialise the SDK."""
    with mock.patch.dict(os.environ, {"SENTRY_DSN": ""}):
        return importlib.import_module("daiv.settings.components.sentry")


scrub_telegram_token = _component().scrub_telegram_token
scrub_payload = _component().scrub_payload


class TestScrubTelegramToken:
    def test_the_token_is_replaced_in_a_bot_api_url(self):
        scrubbed = scrub_telegram_token(TOKEN_URL)
        assert "8123456789:AAH-real-looking-token_x" not in scrubbed
        assert scrubbed == "https://api.telegram.org/bot[REDACTED]/sendMessage"

    def test_the_method_after_the_token_survives(self):
        assert scrub_telegram_token("https://api.telegram.org/bot1:A/setWebhook").endswith("/setWebhook")

    @pytest.mark.parametrize(
        "text", ["", "https://example.com/health", "https://gitlab.com/api/v4/projects", "nothing to see here"]
    )
    def test_unrelated_text_is_untouched(self, text):
        assert scrub_telegram_token(text) == text

    def test_a_url_embedded_in_a_longer_string_is_still_scrubbed(self):
        message = f"POST {TOKEN_URL} failed"
        assert "AAH-real-looking-token_x" not in scrub_telegram_token(message)

    def test_a_quoted_url_stops_at_the_quote(self):
        # Frame locals reach Sentry repr'd, so the token is wrapped in quotes there.
        assert scrub_telegram_token(f"url='{TOKEN_URL}'") == "url='https://api.telegram.org/bot[REDACTED]/sendMessage'"


class TestScrubPayload:
    def test_a_breadcrumb_url_is_scrubbed(self):
        crumb = {"type": "http", "category": "httplib", "data": {"url": TOKEN_URL, "http.method": "POST"}}
        assert scrub_payload(crumb)["data"]["url"] == "https://api.telegram.org/bot[REDACTED]/sendMessage"
        assert scrub_payload(crumb)["data"]["http.method"] == "POST"

    def test_a_span_description_and_data_are_scrubbed(self):
        event = {"spans": [{"description": f"POST {TOKEN_URL}", "data": {"url": TOKEN_URL}}]}
        scrubbed = scrub_payload(event)
        assert "AAH-real-looking-token_x" not in str(scrubbed)

    def test_an_exception_value_and_a_frame_local_are_scrubbed(self):
        event = {
            "exception": {
                "values": [
                    {"value": f"ConnectError for {TOKEN_URL}", "stacktrace": {"frames": [{"vars": {"url": TOKEN_URL}}]}}
                ]
            }
        }
        assert "AAH-real-looking-token_x" not in str(scrub_payload(event))

    def test_non_string_leaves_survive_unchanged(self):
        sentinel = object()
        event = {"level": "error", "count": 3, "ok": True, "obj": sentinel, "tags": None}
        assert scrub_payload(event) == {"level": "error", "count": 3, "ok": True, "obj": sentinel, "tags": None}

    def test_tuples_and_nested_lists_are_walked(self):
        assert "AAH-real-looking-token_x" not in str(scrub_payload({"a": [("x", TOKEN_URL)]}))

    def test_a_self_referential_payload_terminates(self):
        # Frame locals are not JSON yet when before_send runs, so a cycle is reachable.
        event: dict = {"url": TOKEN_URL}
        event["self"] = event
        scrubbed = scrub_payload(event)
        assert scrubbed["url"] == "https://api.telegram.org/bot[REDACTED]/sendMessage"
