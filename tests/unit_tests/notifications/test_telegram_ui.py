from __future__ import annotations

from django.urls import reverse
from django.utils import timezone

import pytest
from notifications.choices import ChannelType
from notifications.models import UserChannelBinding
from notifications.views import ChannelConnect

URL = "/accounts/channels/"

pytestmark = pytest.mark.django_db


def _action(url_name: str) -> str:
    """A form-action match. The disconnect route is the connect route plus a suffix, so a bare
    substring match on the connect URL is also satisfied by a Disconnect form."""
    return f'action="{reverse(url_name)}"'


class TestDescriptor:
    def test_every_entry_is_a_named_descriptor(self):
        from notifications.views import _CHANNEL_CONNECT

        assert all(isinstance(value, ChannelConnect) for value in _CHANNEL_CONNECT.values())

    def test_rocket_chat_keeps_the_text_input_style(self):
        from notifications.views import _CHANNEL_CONNECT

        row = _CHANNEL_CONNECT[ChannelType.ROCKETCHAT]
        assert row.style == "input"
        assert row.placeholder == "@username"

    def test_telegram_connects_by_link(self):
        from notifications.views import _CHANNEL_CONNECT

        assert _CHANNEL_CONNECT[ChannelType.TELEGRAM].style == "link"


@pytest.mark.usefixtures("telegram_channel_enabled")
class TestTelegramRow:
    def test_renders_a_link_style_connect_button_and_no_text_input(self, member_client):
        # Only email and Telegram are enabled here, and neither takes a typed address, so the
        # username input must be absent from the whole page.
        content = member_client.get(URL).content.decode()
        assert "Telegram" in content
        assert reverse("notifications:telegram_connect") in content
        assert 'name="username"' not in content

    def test_renders_the_disconnect_form_and_the_handle_when_bound(self, member_client, member_user):
        UserChannelBinding.objects.create(
            user=member_user,
            channel_type=ChannelType.TELEGRAM,
            address="555",
            extra_config={"handle": "alice"},
            is_verified=True,
            verified_at=timezone.now(),
        )
        content = member_client.get(URL).content.decode()
        assert reverse("notifications:telegram_disconnect") in content
        assert "Disconnect" in content
        assert "alice" in content

    def test_a_verified_row_offers_no_connect_control(self, member_client, member_user):
        UserChannelBinding.objects.create(
            user=member_user,
            channel_type=ChannelType.TELEGRAM,
            address="555",
            is_verified=True,
            verified_at=timezone.now(),
        )
        content = member_client.get(URL).content.decode()
        assert _action("notifications:telegram_connect") not in content
        assert _action("notifications:telegram_disconnect") in content

    def test_an_unverified_row_keeps_a_reachable_connect_control(self, member_client, member_user):
        # unverify_binding leaves the row in place so the page can prompt a reconnect; without a
        # connect control beside Disconnect there is no way back short of deleting the row first.
        UserChannelBinding.objects.create(
            user=member_user,
            channel_type=ChannelType.TELEGRAM,
            address="555",
            extra_config={"handle": "alice"},
            is_verified=False,
        )
        content = member_client.get(URL).content.decode()
        assert "Unverified" in content
        assert _action("notifications:telegram_connect") in content
        assert _action("notifications:telegram_disconnect") in content

    def test_an_unverified_row_still_respects_the_connect_ready_gate(
        self, member_client, member_user, site_settings_override
    ):
        site_settings_override(telegram_enabled=True, telegram_bot_username=None)
        UserChannelBinding.objects.create(
            user=member_user, channel_type=ChannelType.TELEGRAM, address="555", is_verified=False
        )
        content = member_client.get(URL).content.decode()
        assert _action("notifications:telegram_connect") not in content
        assert "Not ready" in content
        assert _action("notifications:telegram_disconnect") in content

    def test_text_input_channels_are_unchanged(self, member_client, rocketchat_channel_enabled):
        content = member_client.get(URL).content.decode()
        assert 'name="username"' in content
        assert "@username" in content


class TestNotReadyRow:
    def test_the_connect_control_is_hidden_while_the_bot_username_is_blank(self, member_client, site_settings_override):
        # With no derived username the deep link would be https://t.me/?start=… — a dead link.

        site_settings_override(telegram_enabled=True, telegram_bot_username=None)
        content = member_client.get(URL).content.decode()
        assert "Telegram" in content
        assert reverse("notifications:telegram_connect") not in content
        assert "Not ready" in content


class TestDisabledChannel:
    def test_the_row_is_absent_when_telegram_is_disabled(self, member_client):
        assert "Telegram" not in member_client.get(URL).content.decode()


@pytest.mark.usefixtures("telegram_channel_enabled")
class TestConnectView:
    def test_redirects_to_the_deep_link_carrying_a_fresh_token(self, member_client, member_user):
        from notifications.telegram.tokens import peek_user_pk

        response = member_client.post(reverse("notifications:telegram_connect"))
        assert response.status_code == 302
        assert response.url.startswith("https://t.me/daiv_bot?start=")
        assert peek_user_pk(response.url.split("start=")[1]) == member_user.pk

    def test_get_is_rejected(self, member_client):
        assert member_client.get(reverse("notifications:telegram_connect")).status_code == 405

    def test_requires_login(self, client):
        assert client.post(reverse("notifications:telegram_connect")).status_code == 302

    def test_the_bot_username_is_percent_encoded_into_the_deep_link(self, member_client, site_settings_override):
        # Only getMe or the env var writes this field, so it is trusted today — quoting removes
        # the assumption rather than relying on it.
        site_settings_override(telegram_enabled=True, telegram_bot_username="ev/il bot")
        response = member_client.post(reverse("notifications:telegram_connect"))
        assert response.url.startswith("https://t.me/ev%2Fil%20bot?start=")

    def test_a_blank_bot_username_flashes_an_error_instead_of_a_dead_link(self, member_client, site_settings_override):
        site_settings_override(telegram_bot_username=None)
        response = member_client.post(reverse("notifications:telegram_connect"))
        assert response.status_code == 302
        assert response.url == URL


class TestConnectViewWhenDisabled:
    def test_404s_when_the_channel_is_disabled(self, member_client):
        assert member_client.post(reverse("notifications:telegram_connect")).status_code == 404


@pytest.mark.usefixtures("telegram_channel_enabled")
class TestDisconnectView:
    def test_deletes_the_binding_and_redirects(self, member_client, member_user):
        UserChannelBinding.objects.create(
            user=member_user,
            channel_type=ChannelType.TELEGRAM,
            address="555",
            is_verified=True,
            verified_at=timezone.now(),
        )
        response = member_client.post(reverse("notifications:telegram_disconnect"))
        assert response.status_code == 302
        assert not UserChannelBinding.objects.filter(user=member_user, channel_type=ChannelType.TELEGRAM).exists()

    def test_requires_login(self, client):
        assert client.post(reverse("notifications:telegram_disconnect")).status_code == 302

    def test_leaves_other_users_bindings_alone(self, member_client, member_user, admin_user):
        UserChannelBinding.objects.create(
            user=admin_user,
            channel_type=ChannelType.TELEGRAM,
            address="777",
            is_verified=True,
            verified_at=timezone.now(),
        )
        member_client.post(reverse("notifications:telegram_disconnect"))
        assert UserChannelBinding.objects.filter(user=admin_user, channel_type=ChannelType.TELEGRAM).exists()
