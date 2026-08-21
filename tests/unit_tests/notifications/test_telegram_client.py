from __future__ import annotations

import httpx
import pytest
from notifications.telegram.client import TelegramPermanentError, TGClient, _tg_post, is_blocked_error

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
        assert _tg_post(CLIENT, "sendMessage", {"chat_id": "1", "text": "hi"})["result"]["message_id"] == 7

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_non_429_4xx_is_permanent(self, httpx_mock, status):
        httpx_mock.add_response(
            method="POST",
            url=SEND_URL,
            json={"ok": False, "error_code": status, "description": "Bad Request: chat not found"},
            status_code=status,
        )
        with pytest.raises(TelegramPermanentError) as exc:
            _tg_post(CLIENT, "sendMessage", {"chat_id": "1", "text": "hi"})
        assert "chat not found" in str(exc.value)

    @pytest.mark.parametrize("status", [429, 500, 502, 503])
    def test_429_and_5xx_are_transient(self, httpx_mock, status):
        httpx_mock.add_response(
            method="POST",
            url=SEND_URL,
            json={"ok": False, "error_code": status, "description": "busy"},
            status_code=status,
        )
        with pytest.raises(RuntimeError):
            _tg_post(CLIENT, "sendMessage", {"chat_id": "1", "text": "hi"})

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
            _tg_post(CLIENT, "sendMessage", {"chat_id": "1", "text": "hi"})

    def test_retryable_error_envelope_inside_a_2xx_stays_transient(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url=SEND_URL,
            json={"ok": False, "error_code": 429, "description": "Too Many Requests"},
            status_code=200,
        )
        with pytest.raises(RuntimeError) as exc:
            _tg_post(CLIENT, "sendMessage", {"chat_id": "1", "text": "hi"})
        assert not isinstance(exc.value, TelegramPermanentError)

    def test_transport_error_propagates_for_retry(self, httpx_mock):
        httpx_mock.add_exception(httpx.ConnectError("no route to host"))
        with pytest.raises(httpx.RequestError):
            _tg_post(CLIENT, "sendMessage", {"chat_id": "1", "text": "hi"})

    def test_permanent_error_without_a_description_falls_back_to_the_status(self, httpx_mock):
        httpx_mock.add_response(method="POST", url=SEND_URL, content=b"<html>nope</html>", status_code=400)
        with pytest.raises(TelegramPermanentError) as exc:
            _tg_post(CLIENT, "sendMessage", {"chat_id": "1", "text": "hi"})
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


class TestIsBlockedError:
    @pytest.mark.parametrize(
        "description", ["Forbidden: bot was blocked by the user", "forbidden: BOT WAS BLOCKED BY THE USER"]
    )
    def test_matches_the_blocked_wording(self, description):
        assert is_blocked_error(description) is True

    @pytest.mark.parametrize("description", ["Forbidden: user is deactivated", "Bad Request: chat not found", ""])
    def test_does_not_match_other_permanent_failures(self, description):
        assert is_blocked_error(description) is False
