from __future__ import annotations

import json

from django.utils import timezone

import httpx
import pytest
from notifications.channels.registry import enabled_channels, get_channel
from notifications.channels.telegram import TelegramChannel
from notifications.choices import ChannelType, EventType
from notifications.exceptions import UnrecoverableDeliveryError
from notifications.models import UserChannelBinding
from notifications.telegram.client import TelegramTransportError

SEND_URL = "https://api.telegram.org/bot123:ABC/sendMessage"


class TestRegistration:
    def test_channel_is_registered(self):
        assert get_channel(ChannelType.TELEGRAM).channel_type == ChannelType.TELEGRAM

    def test_appears_in_enabled_channels_only_when_enabled(self, site_settings_override):
        site_settings_override(telegram_enabled=True)
        assert TelegramChannel in enabled_channels()
        site_settings_override(telegram_enabled=False)
        assert TelegramChannel not in enabled_channels()


@pytest.mark.django_db
class TestResolveAddress:
    def test_returns_the_chat_id_of_a_verified_binding(self, member_user):
        UserChannelBinding.objects.create(
            user=member_user,
            channel_type=ChannelType.TELEGRAM,
            address="555",
            is_verified=True,
            verified_at=timezone.now(),
        )
        assert TelegramChannel().resolve_address(member_user) == "555"

    def test_an_unverified_binding_resolves_to_none_so_delivery_is_skipped(self, member_user):
        UserChannelBinding.objects.create(
            user=member_user, channel_type=ChannelType.TELEGRAM, address="555", is_verified=False
        )
        assert TelegramChannel().resolve_address(member_user) is None


@pytest.mark.django_db
class TestSend:
    def _delivery(self, notification_with_delivery, address="555"):
        n, d = notification_with_delivery
        d.channel_type = ChannelType.TELEGRAM
        d.address = address
        d.save()
        return n, d

    def test_happy_path_posts_html_with_an_inline_keyboard(
        self, httpx_mock, notification_with_delivery, telegram_configured
    ):
        n, d = self._delivery(notification_with_delivery)
        n.event_type = EventType.SCHEDULE_FINISHED
        n.subject, n.link_url = "Nightly needs a look", "/dashboard/sessions/1/"
        n.context = {"status_tone": "warning", "repo_id": "acme/api", "trigger_owner": "alice", "duration_seconds": 47}
        n.save()

        httpx_mock.add_response(method="POST", url=SEND_URL, json={"ok": True, "result": {}}, status_code=200)
        TelegramChannel().send(n, d)

        body = json.loads(httpx_mock.get_requests()[0].content)
        assert body["chat_id"] == "555"
        assert body["parse_mode"] == "HTML"
        assert "Nightly needs a look" in body["text"]
        assert body["reply_markup"]["inline_keyboard"][0][0]["url"].endswith("/dashboard/sessions/1/")

    def test_unknown_event_type_falls_back_to_plain_text_without_a_parse_mode(
        self, httpx_mock, notification_with_delivery, telegram_configured
    ):
        # No parse_mode, so the fallback needs no escaping and cannot 400 on stray markup.
        n, d = self._delivery(notification_with_delivery)
        n.event_type = "some.future.event"
        n.subject, n.body, n.link_url = "Subject <b>", "Body line", "/x/"
        n.save()

        httpx_mock.add_response(method="POST", url=SEND_URL, json={"ok": True, "result": {}}, status_code=200)
        TelegramChannel().send(n, d)

        body = json.loads(httpx_mock.get_requests()[0].content)
        assert "parse_mode" not in body
        assert "Subject <b>" in body["text"]
        assert "Body line" in body["text"]
        assert "reply_markup" not in body

    def test_disabled_channel_raises_unrecoverable(
        self, notification_with_delivery, telegram_configured, site_settings_override
    ):
        site_settings_override(telegram_enabled=False)
        n, d = self._delivery(notification_with_delivery)
        with pytest.raises(UnrecoverableDeliveryError, match="disabled"):
            TelegramChannel().send(n, d)

    def test_missing_token_raises_unrecoverable(
        self, notification_with_delivery, telegram_configured, site_settings_override
    ):
        site_settings_override(telegram_bot_token=None)
        n, d = self._delivery(notification_with_delivery)
        with pytest.raises(UnrecoverableDeliveryError, match="not configured"):
            TelegramChannel().send(n, d)

    @pytest.mark.parametrize("status", [400, 403])
    def test_permanent_errors_raise_unrecoverable(
        self, httpx_mock, notification_with_delivery, telegram_configured, status
    ):
        n, d = self._delivery(notification_with_delivery)
        httpx_mock.add_response(
            method="POST",
            url=SEND_URL,
            json={"ok": False, "error_code": status, "description": "Bad Request: chat not found"},
            status_code=status,
        )
        with pytest.raises(UnrecoverableDeliveryError):
            TelegramChannel().send(n, d)

    @pytest.mark.parametrize("status", [429, 503])
    def test_transient_errors_propagate_for_the_retry_ladder(
        self, httpx_mock, notification_with_delivery, telegram_configured, status
    ):
        n, d = self._delivery(notification_with_delivery)
        httpx_mock.add_response(
            method="POST",
            url=SEND_URL,
            json={"ok": False, "error_code": status, "description": "busy"},
            status_code=status,
        )
        with pytest.raises(RuntimeError):
            TelegramChannel().send(n, d)

    def test_a_transport_error_propagates_for_retry(self, httpx_mock, notification_with_delivery, telegram_configured):
        n, d = self._delivery(notification_with_delivery)
        httpx_mock.add_exception(httpx.ConnectError("no route"))
        with pytest.raises(TelegramTransportError) as exc:
            TelegramChannel().send(n, d)
        # Not an UnrecoverableDeliveryError, so _deliver_notification keeps retrying it.
        assert not isinstance(exc.value, UnrecoverableDeliveryError)


@pytest.mark.django_db
class TestBlockedBotUnverifies:
    def _bound_delivery(self, member_user, notification_with_delivery, address="555"):
        n, d = notification_with_delivery
        d.channel_type = ChannelType.TELEGRAM
        d.address = address
        d.save()
        UserChannelBinding.objects.create(
            user=member_user,
            channel_type=ChannelType.TELEGRAM,
            address=address,
            is_verified=True,
            verified_at=timezone.now(),
        )
        return n, d

    def test_a_blocked_403_flips_the_binding_to_unverified(
        self, httpx_mock, member_user, notification_with_delivery, telegram_configured
    ):
        n, d = self._bound_delivery(member_user, notification_with_delivery)
        httpx_mock.add_response(
            method="POST",
            url=SEND_URL,
            json={"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"},
            status_code=403,
        )
        with pytest.raises(UnrecoverableDeliveryError):
            TelegramChannel().send(n, d)

        binding = UserChannelBinding.objects.get(user=member_user, channel_type=ChannelType.TELEGRAM)
        assert binding.is_verified is False
        assert binding.verified_at is not None  # the CheckConstraint permits the stale timestamp
        assert TelegramChannel().resolve_address(member_user) is None  # later deliveries now skip

    def test_a_non_matching_403_fails_permanently_without_the_flip(
        self, httpx_mock, member_user, notification_with_delivery, telegram_configured
    ):
        n, d = self._bound_delivery(member_user, notification_with_delivery)
        httpx_mock.add_response(
            method="POST",
            url=SEND_URL,
            json={"ok": False, "error_code": 403, "description": "Forbidden: user is deactivated"},
            status_code=403,
        )
        with pytest.raises(UnrecoverableDeliveryError):
            TelegramChannel().send(n, d)
        assert UserChannelBinding.objects.get(user=member_user, channel_type=ChannelType.TELEGRAM).is_verified is True

    def test_the_flip_only_touches_the_delivering_address(
        self, httpx_mock, member_user, notification_with_delivery, telegram_configured
    ):
        # Two Telegram bindings for one user: only the address that got the 403 may flip.
        # user_channel_binding_unique is per-(user, channel_type, address), so this is a legal row pair.
        n, d = self._bound_delivery(member_user, notification_with_delivery, address="555")
        UserChannelBinding.objects.create(
            user=member_user,
            channel_type=ChannelType.TELEGRAM,
            address="999",
            is_verified=True,
            verified_at=timezone.now(),
        )
        httpx_mock.add_response(
            method="POST",
            url=SEND_URL,
            json={"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"},
            status_code=403,
        )
        with pytest.raises(UnrecoverableDeliveryError):
            TelegramChannel().send(n, d)

        rows = UserChannelBinding.objects.filter(user=member_user, channel_type=ChannelType.TELEGRAM)
        assert rows.get(address="555").is_verified is False
        assert rows.get(address="999").is_verified is True
