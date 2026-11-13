from __future__ import annotations

from unittest.mock import patch
from urllib.parse import urlparse

from django.utils import timezone

import pytest
from notifications.choices import ChannelType
from notifications.models import UserChannelBinding

USER_ID = "123456789012345678"


def _request_challenge(member_client):
    with (
        patch("notifications.forms.verify_user_id", return_value=(USER_ID, None)),
        patch("notifications.views._DiscordClient.send_direct_message") as send,
    ):
        response = member_client.post(TestDiscordConnect.URL, {"username": USER_ID}, follow=False)
    assert response.status_code == 302
    send.assert_called_once()
    content = send.call_args.args[-1]["content"]
    return content.rsplit(": ", 1)[-1]


@pytest.mark.django_db
@pytest.mark.usefixtures("discord_channel_enabled")
class TestDiscordConnect:
    URL = "/dashboard/notifications/channels/discord/"

    def test_requires_login(self, client):
        response = client.post(self.URL, {"username": USER_ID})
        assert response.status_code == 302
        assert "/accounts/login" in response["Location"] or "/login" in response["Location"]

    def test_sends_challenge_and_creates_an_unverified_binding(self, member_client, member_user):
        confirmation_url = _request_challenge(member_client)
        assert "/dashboard/notifications/channels/discord/confirm/" in confirmation_url
        binding = UserChannelBinding.objects.get(user=member_user, channel_type=ChannelType.DISCORD)
        assert binding.address == USER_ID
        assert binding.is_verified is False
        assert binding.verified_at is None
        assert set(binding.extra_config) == {"challenge_hash", "challenge_expires_at"}
        assert confirmation_url.rsplit("/", 2)[-2] not in str(binding.extra_config)

    @pytest.mark.parametrize("username", ["alice", "123abc", "１２３４５"])
    def test_non_numeric_id_does_not_call_discord_or_create_a_binding(self, member_client, member_user, username):
        with patch("notifications.forms.verify_user_id") as verify:
            response = member_client.post(self.URL, {"username": username}, follow=True)
        assert response.status_code == 200
        verify.assert_not_called()
        messages = [str(message) for message in response.context["messages"]]
        assert "Enter a valid Discord user ID using digits only." in messages
        assert not UserChannelBinding.objects.filter(user=member_user, channel_type=ChannelType.DISCORD).exists()

    @pytest.mark.parametrize(
        "error", ["Discord user not found.", "Discord is temporarily unavailable. Please try again."]
    )
    def test_verification_failure_saves_nothing(self, member_client, member_user, error):
        with patch("notifications.forms.verify_user_id", return_value=(None, error)):
            response = member_client.post(self.URL, {"username": USER_ID}, follow=True)
        assert response.status_code == 200
        assert error in [str(message) for message in response.context["messages"]]
        assert not UserChannelBinding.objects.filter(user=member_user, channel_type=ChannelType.DISCORD).exists()

    def test_challenge_delivery_failure_saves_nothing(self, member_client, member_user):
        from notifications.channels.discord import DiscordPermanentError

        with (
            patch("notifications.forms.verify_user_id", return_value=(USER_ID, None)),
            patch(
                "notifications.views._DiscordClient.send_direct_message",
                side_effect=DiscordPermanentError("Cannot send messages"),
            ),
        ):
            response = member_client.post(self.URL, {"username": USER_ID}, follow=True)
        assert response.status_code == 200
        assert "Discord could not send the confirmation message. Check the shared server and try again." in [
            str(message) for message in response.context["messages"]
        ]
        assert not UserChannelBinding.objects.filter(user=member_user, channel_type=ChannelType.DISCORD).exists()

    def test_repeated_request_is_throttled_before_calling_discord(self, member_client, member_user):
        _request_challenge(member_client)
        with (
            patch("notifications.forms.verify_user_id") as verify,
            patch("notifications.views._DiscordClient.send_direct_message") as send,
        ):
            response = member_client.post(self.URL, {"username": USER_ID}, follow=True)
        assert response.status_code == 200
        verify.assert_not_called()
        send.assert_not_called()
        assert "Please wait a minute before requesting another Discord confirmation." in [
            str(message) for message in response.context["messages"]
        ]

    def test_empty_user_id_does_not_create_a_binding(self, member_client, member_user):
        response = member_client.post(self.URL, {"username": "  "}, follow=True)
        assert response.status_code == 200
        assert "User ID is required." in [str(message) for message in response.context["messages"]]
        assert not UserChannelBinding.objects.filter(user=member_user, channel_type=ChannelType.DISCORD).exists()


@pytest.mark.django_db
@pytest.mark.usefixtures("discord_channel_enabled")
class TestDiscordConfirm:
    def test_confirmation_verifies_binding_and_consumes_challenge(self, member_client, member_user):
        confirmation_url = _request_challenge(member_client)
        response = member_client.get(urlparse(confirmation_url).path, follow=True)
        assert response.status_code == 200
        assert "Discord connected. Your account is verified." in [
            str(message) for message in response.context["messages"]
        ]
        binding = UserChannelBinding.objects.get(user=member_user, channel_type=ChannelType.DISCORD)
        assert binding.is_verified is True
        assert binding.verified_at is not None
        assert binding.extra_config == {}

        response = member_client.get(urlparse(confirmation_url).path, follow=True)
        assert "This Discord confirmation link is invalid, expired, or already used." in [
            str(message) for message in response.context["messages"]
        ]

    def test_expired_challenge_does_not_verify(self, member_client, member_user):
        confirmation_url = _request_challenge(member_client)
        binding = UserChannelBinding.objects.get(user=member_user, channel_type=ChannelType.DISCORD)
        binding.extra_config["challenge_expires_at"] = timezone.now().timestamp() - 1
        binding.save(update_fields=["extra_config", "modified"])
        response = member_client.get(urlparse(confirmation_url).path, follow=True)
        assert "This Discord confirmation link is invalid, expired, or already used." in [
            str(message) for message in response.context["messages"]
        ]
        binding.refresh_from_db()
        assert binding.is_verified is False

    def test_another_user_cannot_consume_the_challenge(self, member_client, admin_client, member_user):
        confirmation_url = _request_challenge(member_client)
        response = admin_client.get(urlparse(confirmation_url).path, follow=True)
        assert "This Discord confirmation link is invalid, expired, or already used." in [
            str(message) for message in response.context["messages"]
        ]
        binding = UserChannelBinding.objects.get(user=member_user, channel_type=ChannelType.DISCORD)
        assert binding.is_verified is False
        assert binding.extra_config["challenge_hash"]


@pytest.mark.django_db
def test_connect_returns_404_when_channel_disabled(member_client):
    response = member_client.post("/dashboard/notifications/channels/discord/", {"username": USER_ID})
    assert response.status_code == 404


@pytest.mark.django_db
class TestDiscordDisconnect:
    URL = "/dashboard/notifications/channels/discord/delete/"

    def test_deletes_existing_binding(self, member_client, member_user):
        UserChannelBinding.objects.create(
            user=member_user,
            channel_type=ChannelType.DISCORD,
            address=USER_ID,
            is_verified=True,
            verified_at=timezone.now(),
        )
        response = member_client.post(self.URL, follow=False)
        assert response.status_code == 302
        assert not UserChannelBinding.objects.filter(user=member_user, channel_type=ChannelType.DISCORD).exists()

    def test_is_idempotent_when_no_binding(self, member_client):
        response = member_client.post(self.URL, follow=False)
        assert response.status_code == 302
