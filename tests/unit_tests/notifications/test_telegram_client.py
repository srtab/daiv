from __future__ import annotations

import traceback

import httpx
import pytest
from notifications.telegram.client import (
    TelegramPermanentError,
    TelegramTransientError,
    TelegramTransportError,
    TGClient,
    is_unreachable_chat_error,
)

CLIENT = TGClient(token="123:ABC")  # noqa: S106 — test constant
SEND_URL = "https://api.telegram.org/bot123:ABC/sendMessage"


class TestFromSiteSettings:
    def test_returns_none_without_a_token(self, site_settings_override):
        site_settings_override(telegram_bot_token=None)
        assert TGClient.from_site_settings() is None

    def test_unwraps_the_secret_str(self, telegram_configured):
        client = TGClient.from_site_settings()
        assert client is not None
        assert client.token == "123:ABC"  # noqa: S105 — test constant

    def test_repr_does_not_leak_the_token(self):
        assert "123:ABC" not in repr(CLIENT)


class TestTgPost:
    def test_ok_envelope_is_returned_parsed(self, httpx_mock):
        httpx_mock.add_response(
            method="POST", url=SEND_URL, json={"ok": True, "result": {"message_id": 7}}, status_code=200
        )
        assert CLIENT.call("sendMessage", {"chat_id": "1", "text": "hi"})["result"]["message_id"] == 7

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_non_429_4xx_is_permanent(self, httpx_mock, status):
        httpx_mock.add_response(
            method="POST",
            url=SEND_URL,
            json={"ok": False, "error_code": status, "description": "Bad Request: chat not found"},
            status_code=status,
        )
        with pytest.raises(TelegramPermanentError) as exc:
            CLIENT.call("sendMessage", {"chat_id": "1", "text": "hi"})
        assert "chat not found" in str(exc.value)

    @pytest.mark.parametrize("status", [429, 500, 502, 503])
    def test_429_and_5xx_are_transient(self, httpx_mock, status):
        httpx_mock.add_response(
            method="POST",
            url=SEND_URL,
            json={"ok": False, "error_code": status, "description": "busy"},
            status_code=status,
        )
        with pytest.raises(TelegramTransientError):
            CLIENT.call("sendMessage", {"chat_id": "1", "text": "hi"})

    def test_error_envelope_inside_a_2xx_is_classified_too(self, httpx_mock):
        # Telegram normally mirrors error_code into the HTTP status, but the envelope is
        # authoritative — a 200 carrying ok=false must not read as a success.
        httpx_mock.add_response(
            method="POST",
            url=SEND_URL,
            json={"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"},
            status_code=200,
        )
        with pytest.raises(TelegramPermanentError):
            CLIENT.call("sendMessage", {"chat_id": "1", "text": "hi"})

    def test_retryable_error_envelope_inside_a_2xx_stays_transient(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url=SEND_URL,
            json={"ok": False, "error_code": 429, "description": "Too Many Requests"},
            status_code=200,
        )
        with pytest.raises(TelegramTransientError) as exc:
            CLIENT.call("sendMessage", {"chat_id": "1", "text": "hi"})
        assert not isinstance(exc.value, TelegramPermanentError)

    def test_transport_error_propagates_for_retry(self, httpx_mock):
        httpx_mock.add_exception(httpx.ConnectError("no route to host"))
        with pytest.raises(TelegramTransportError) as exc:
            CLIENT.call("sendMessage", {"chat_id": "1", "text": "hi"})
        # Transient: a permanent error would stop the delivery task's three-attempt ladder.
        assert not isinstance(exc.value, TelegramPermanentError)
        assert isinstance(exc.value, Exception)

    def test_a_transport_error_keeps_no_httpx_frames_and_names_no_url(self, httpx_mock):
        # httpx's own frames hold the request URL in their locals, and the Bot API puts the
        # token in that URL's path — a captured traceback would ship the credential.
        httpx_mock.add_exception(httpx.ConnectError("no route to host"))
        with pytest.raises(TelegramTransportError) as exc:
            CLIENT.call("sendMessage", {"chat_id": "1", "text": "hi"})
        assert exc.value.__cause__ is None
        assert exc.value.__suppress_context__ is True
        assert "123:ABC" not in str(exc.value)
        assert not [frame for frame in traceback.extract_tb(exc.value.__traceback__) if "httpx" in frame.filename]

    def test_a_non_json_2xx_is_transient(self, httpx_mock):
        # A proxy or captive portal answering 200 with HTML is not a Bot API verdict; filing it
        # as permanent would drop the notification, and a bare ValueError would too.
        httpx_mock.add_response(method="POST", url=SEND_URL, content=b"<html>hi</html>", status_code=200)
        with pytest.raises(TelegramTransportError) as exc:
            CLIENT.call("sendMessage", {"chat_id": "1", "text": "hi"})
        assert not isinstance(exc.value, TelegramPermanentError)
        assert "123:ABC" not in str(exc.value)

    def test_permanent_error_without_a_description_falls_back_to_the_status(self, httpx_mock):
        httpx_mock.add_response(method="POST", url=SEND_URL, content=b"<html>nope</html>", status_code=400)
        with pytest.raises(TelegramPermanentError) as exc:
            CLIENT.call("sendMessage", {"chat_id": "1", "text": "hi"})
        assert "HTTP 400" in str(exc.value)

    @pytest.mark.parametrize("body", [[], "blocked", 3, [{"ok": True}]])
    def test_a_2xx_carrying_non_object_json_is_transient_not_an_attribute_error(self, httpx_mock, body):
        # A portal answering 200 with valid-but-non-object JSON parses fine, so the ValueError
        # guard misses it and ``body.get`` used to raise AttributeError — escaping the
        # permanent/transient classification into whichever broad handler was upstream.
        httpx_mock.add_response(method="POST", url=SEND_URL, json=body, status_code=200)
        with pytest.raises(TelegramTransportError) as exc:
            CLIENT.call("sendMessage", {"chat_id": "1", "text": "hi"})
        assert not isinstance(exc.value, TelegramPermanentError)
        assert "123:ABC" not in str(exc.value)

    @pytest.mark.parametrize("body", [[], "blocked"])
    def test_a_4xx_carrying_non_object_json_still_classifies_as_permanent(self, httpx_mock, body):
        # Same gap on the error path: _extract_tg_error called .get on whatever parsed.
        httpx_mock.add_response(method="POST", url=SEND_URL, json=body, status_code=400)
        with pytest.raises(TelegramPermanentError) as exc:
            CLIENT.call("sendMessage", {"chat_id": "1", "text": "hi"})
        assert "HTTP 400" in str(exc.value)


class TestConvenienceMethods:
    def test_get_me_hits_the_getme_method(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="https://api.telegram.org/bot123:ABC/getMe",
            json={"ok": True, "result": {"username": "daiv_bot"}},
            status_code=200,
        )
        assert CLIENT.get_me()["result"]["username"] == "daiv_bot"

    def test_set_webhook_sends_url_secret_and_the_allowed_update_types(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="https://api.telegram.org/bot123:ABC/setWebhook",
            json={"ok": True, "result": True},
            status_code=200,
            match_json={
                "url": "https://daiv.test/api/notifications/callbacks/telegram/",
                "secret_token": "s3cret",
                "allowed_updates": ["message", "my_chat_member"],
            },
        )
        CLIENT.set_webhook(url="https://daiv.test/api/notifications/callbacks/telegram/", secret_token="s3cret")  # noqa: S106 — test constant

    def test_delete_webhook_and_get_webhook_info(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="https://api.telegram.org/bot123:ABC/deleteWebhook",
            json={"ok": True, "result": True},
            status_code=200,
        )
        httpx_mock.add_response(
            method="POST",
            url="https://api.telegram.org/bot123:ABC/getWebhookInfo",
            json={"ok": True, "result": {"url": "", "pending_update_count": 0}},
            status_code=200,
        )
        assert CLIENT.delete_webhook()["result"] is True
        assert CLIENT.get_webhook_info()["result"]["url"] == ""


class TestIsUnreachableChatError:
    @pytest.mark.parametrize(
        "description",
        [
            "Forbidden: bot was blocked by the user",
            "forbidden: BOT WAS BLOCKED BY THE USER",
            # A deleted Telegram account and a vanished chat never recover either, so they earn
            # the same binding flip — keeping the row verified only buys three retries per
            # notification forever, behind a UI that still reads "Verified".
            "Forbidden: user is deactivated",
            "Bad Request: chat not found",
        ],
    )
    def test_matches_every_permanently_unreachable_wording(self, description):
        assert is_unreachable_chat_error(description) is True

    @pytest.mark.parametrize(
        "description",
        [
            # Permanent, but about the *message* — the chat is fine and must stay verified.
            "Bad Request: message is too long",
            "Bad Request: can't parse entities",
            "",
        ],
    )
    def test_does_not_match_failures_the_chat_can_recover_from(self, description):
        assert is_unreachable_chat_error(description) is False
