from __future__ import annotations

import json

import httpx
import pytest
from notifications.channels.discord import (
    DiscordChannel,
    DiscordPermanentError,
    DiscordTransientError,
    _DiscordClient,
    _extract_retry_after,
    verify_user_id,
)
from notifications.channels.registry import get_channel
from notifications.choices import ChannelType
from notifications.exceptions import UnrecoverableDeliveryError

USER_ID = "123456789012345678"
USER_URL = f"https://discord.com/api/v10/users/{USER_ID}"
DM_URL = "https://discord.com/api/v10/users/@me/channels"
MESSAGE_URL = "https://discord.com/api/v10/channels/dm-channel/messages"


def test_channel_is_registered():
    assert get_channel(ChannelType.DISCORD).channel_type == ChannelType.DISCORD


@pytest.mark.django_db
def test_connect_is_blocked_without_a_bot_token(site_settings_override):
    site_settings_override(discord_bot_token=None)
    assert DiscordChannel.connect_ready() is False
    assert "administrator" in DiscordChannel.connect_blocked_reason()


class TestVerifyUserId:
    def test_success_returns_user_id_and_sends_bot_authorization(self, httpx_mock, discord_configured):
        httpx_mock.add_response(
            method="GET",
            url=USER_URL,
            json={"id": USER_ID, "username": "alice"},
            match_headers={"Authorization": "Bot bottoken"},
        )
        assert verify_user_id(USER_ID) == (USER_ID, None)

    def test_unknown_user_returns_an_error(self, httpx_mock, discord_configured):
        httpx_mock.add_response(method="GET", url=USER_URL, status_code=404, json={"message": "Unknown User"})
        user_id, error = verify_user_id(USER_ID)
        assert user_id is None
        assert error == "Discord user not found."

    def test_not_configured_returns_an_error(self, site_settings_override):
        site_settings_override(discord_bot_token=None)
        assert verify_user_id(USER_ID) == (None, "Discord is not configured.")

    def test_transport_error_returns_a_generic_error(self, httpx_mock, discord_configured):
        httpx_mock.add_exception(httpx.ConnectError("connection refused"))
        user_id, error = verify_user_id(USER_ID)
        assert user_id is None
        assert error == "Discord is temporarily unavailable. Please try again."

    @pytest.mark.parametrize("status_code", [400, 401, 403, 429, 503])
    def test_http_failure_does_not_verify_the_user(self, httpx_mock, discord_configured, status_code):
        httpx_mock.add_response(method="GET", url=USER_URL, status_code=status_code)
        user_id, error = verify_user_id(USER_ID)
        assert user_id is None
        assert error == "Discord is temporarily unavailable. Please try again."

    @pytest.mark.parametrize("content", [b"<html>oops</html>", b"[]"])
    def test_non_object_response_returns_an_error(self, httpx_mock, discord_configured, content):
        httpx_mock.add_response(method="GET", url=USER_URL, content=content)
        assert verify_user_id(USER_ID) == (None, "Discord returned an invalid response.")

    def test_mismatched_user_id_returns_an_error(self, httpx_mock, discord_configured):
        httpx_mock.add_response(method="GET", url=USER_URL, json={"id": "999"})
        assert verify_user_id(USER_ID) == (None, "Discord returned an invalid response.")


class TestClientCall:
    CLIENT = _DiscordClient(token="bottoken")  # noqa: S106 — test constant

    @pytest.mark.parametrize("status_code", [400, 401, 403, 404])
    def test_4xx_is_permanent(self, httpx_mock, status_code):
        httpx_mock.add_response(method="POST", url=DM_URL, status_code=status_code, json={"message": "Cannot deliver"})
        with pytest.raises(DiscordPermanentError, match="Cannot deliver"):
            self.CLIENT.call("POST", "/users/@me/channels", {"recipient_id": USER_ID})

    def test_cannot_message_user_is_permanent(self, httpx_mock):
        httpx_mock.add_response(
            method="POST", url=DM_URL, status_code=403, json={"code": 50007, "message": "Cannot send messages"}
        )
        with pytest.raises(DiscordPermanentError, match="Cannot send messages"):
            self.CLIENT.call("POST", "/users/@me/channels", {"recipient_id": USER_ID})

    def test_rate_limit_is_transient_and_carries_discords_delay(self, httpx_mock):
        httpx_mock.add_response(
            method="POST", url=DM_URL, status_code=429, json={"message": "Rate limited", "retry_after": 12.5}
        )
        with pytest.raises(DiscordTransientError) as exc:
            self.CLIENT.call("POST", "/users/@me/channels", {"recipient_id": USER_ID})
        assert exc.value.retry_after == 12.5

    @pytest.mark.parametrize("status_code", [500, 502, 503])
    def test_5xx_is_transient(self, httpx_mock, status_code):
        httpx_mock.add_response(method="POST", url=DM_URL, status_code=status_code)
        with pytest.raises(DiscordTransientError):
            self.CLIENT.call("POST", "/users/@me/channels", {"recipient_id": USER_ID})

    def test_non_object_success_response_is_transient(self, httpx_mock):
        httpx_mock.add_response(method="POST", url=DM_URL, content=b"<html>proxy</html>")
        with pytest.raises(DiscordTransientError):
            self.CLIENT.call("POST", "/users/@me/channels", {"recipient_id": USER_ID})

    def test_transport_error_hides_the_httpx_cause(self, httpx_mock):
        httpx_mock.add_exception(httpx.ConnectError("connection refused"))
        with pytest.raises(DiscordTransientError) as exc:
            self.CLIENT.call("POST", "/users/@me/channels", {"recipient_id": USER_ID})
        assert exc.value.__cause__ is None
        assert exc.value.__suppress_context__ is True


@pytest.mark.parametrize(
    ("value", "expected"), [(12, 12.0), (0.25, 0.25), (True, None), ("12", None), (0, None), (-1, None), (None, None)]
)
def test_extract_retry_after(value, expected):
    assert _extract_retry_after({"retry_after": value}) == expected


@pytest.mark.django_db
class TestSend:
    def test_opens_a_dm_then_sends_one_embed(self, httpx_mock, notification_with_delivery, discord_configured):
        notification, delivery = notification_with_delivery
        delivery.channel_type = ChannelType.DISCORD
        delivery.address = USER_ID
        delivery.save()
        notification.subject = "Subject"
        notification.body = "Body line"
        notification.context = {"is_successful": True, "repo_id": "acme/api", "trigger_owner": "alice"}
        notification.save()
        httpx_mock.add_response(
            method="POST",
            url=DM_URL,
            json={"id": "dm-channel"},
            match_json={"recipient_id": USER_ID},
            match_headers={"Authorization": "Bot bottoken"},
        )
        httpx_mock.add_response(
            method="POST", url=MESSAGE_URL, json={"id": "message-id"}, match_headers={"Authorization": "Bot bottoken"}
        )

        DiscordChannel().send(notification, delivery)

        message = json.loads(httpx_mock.get_requests()[1].content)
        assert len(message["embeds"]) == 1
        assert message["embeds"][0]["title"] == "Subject"
        assert message["embeds"][0]["color"]
        assert "Body line" not in json.dumps(message)

    def test_missing_dm_channel_id_is_transient_and_sends_no_message(
        self, httpx_mock, notification_with_delivery, discord_configured
    ):
        notification, delivery = notification_with_delivery
        httpx_mock.add_response(method="POST", url=DM_URL, json={})
        with pytest.raises(DiscordTransientError, match="channel ID"):
            DiscordChannel().send(notification, delivery)
        assert len(httpx_mock.get_requests()) == 1

    def test_unknown_event_falls_back_to_plain_text(self, httpx_mock, notification_with_delivery, discord_configured):
        notification, delivery = notification_with_delivery
        delivery.channel_type = ChannelType.DISCORD
        delivery.address = USER_ID
        delivery.save()
        notification.event_type = "some.future.event"
        notification.subject = "Subject"
        notification.body = "Body line"
        notification.save()
        httpx_mock.add_response(method="POST", url=DM_URL, json={"id": "dm-channel"})
        httpx_mock.add_response(method="POST", url=MESSAGE_URL, json={"id": "message-id"})

        DiscordChannel().send(notification, delivery)

        message = json.loads(httpx_mock.get_requests()[1].content)
        assert "Subject" in message["content"]
        assert "Body line" in message["content"]
        assert "embeds" not in message

    def test_plain_text_fallback_respects_discords_limit(
        self, httpx_mock, notification_with_delivery, discord_configured
    ):
        notification, delivery = notification_with_delivery
        delivery.channel_type = ChannelType.DISCORD
        delivery.address = USER_ID
        delivery.save()
        notification.event_type = "some.future.event"
        notification.body = "x" * 2500
        notification.save()
        httpx_mock.add_response(method="POST", url=DM_URL, json={"id": "dm-channel"})
        httpx_mock.add_response(method="POST", url=MESSAGE_URL, json={"id": "message-id"})

        DiscordChannel().send(notification, delivery)

        message = json.loads(httpx_mock.get_requests()[1].content)
        assert len(message["content"]) == 2000
        assert message["content"].endswith("…")

    def test_disabled_channel_is_permanent(self, notification_with_delivery, site_settings_override):
        site_settings_override(discord_enabled=False)
        notification, delivery = notification_with_delivery
        with pytest.raises(UnrecoverableDeliveryError, match="disabled"):
            DiscordChannel().send(notification, delivery)

    def test_missing_token_is_permanent(self, notification_with_delivery, site_settings_override):
        site_settings_override(discord_enabled=True, discord_bot_token=None)
        notification, delivery = notification_with_delivery
        with pytest.raises(UnrecoverableDeliveryError, match="not configured"):
            DiscordChannel().send(notification, delivery)

    def test_discord_permanent_error_becomes_unrecoverable(
        self, httpx_mock, notification_with_delivery, discord_configured
    ):
        notification, delivery = notification_with_delivery
        delivery.address = USER_ID
        httpx_mock.add_response(
            method="POST", url=DM_URL, status_code=403, json={"code": 50007, "message": "Cannot send messages"}
        )
        with pytest.raises(UnrecoverableDeliveryError, match="Cannot send messages"):
            DiscordChannel().send(notification, delivery)

    def test_rate_limit_propagates_for_retry(self, httpx_mock, notification_with_delivery, discord_configured):
        notification, delivery = notification_with_delivery
        delivery.address = USER_ID
        httpx_mock.add_response(
            method="POST", url=DM_URL, status_code=429, json={"message": "Rate limited", "retry_after": 7.5}
        )
        with pytest.raises(DiscordTransientError) as exc:
            DiscordChannel().send(notification, delivery)
        assert exc.value.retry_after == 7.5

    def test_server_error_propagates_for_retry(self, httpx_mock, notification_with_delivery, discord_configured):
        notification, delivery = notification_with_delivery
        delivery.address = USER_ID
        httpx_mock.add_response(method="POST", url=DM_URL, status_code=503)
        with pytest.raises(DiscordTransientError):
            DiscordChannel().send(notification, delivery)
