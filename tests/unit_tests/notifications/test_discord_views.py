from __future__ import annotations

from unittest.mock import patch

from django.utils import timezone

import pytest
from notifications.choices import ChannelType
from notifications.models import UserChannelBinding

USER_ID = "123456789012345678"


@pytest.mark.django_db
@pytest.mark.usefixtures("discord_channel_enabled")
class TestDiscordConnect:
    URL = "/dashboard/notifications/channels/discord/"

    def test_requires_login(self, client):
        response = client.post(self.URL, {"username": USER_ID})
        assert response.status_code == 302
        assert "/accounts/login" in response["Location"] or "/login" in response["Location"]

    def test_creates_verified_binding_on_success(self, member_client, member_user):
        with patch("notifications.forms.verify_user_id", return_value=(USER_ID, None)):
            response = member_client.post(self.URL, {"username": USER_ID}, follow=False)
        assert response.status_code == 302
        binding = UserChannelBinding.objects.get(user=member_user, channel_type=ChannelType.DISCORD)
        assert binding.address == USER_ID
        assert binding.is_verified is True
        assert binding.verified_at is not None

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

    def test_empty_user_id_does_not_create_a_binding(self, member_client, member_user):
        response = member_client.post(self.URL, {"username": "  "}, follow=True)
        assert response.status_code == 200
        assert "User ID is required." in [str(message) for message in response.context["messages"]]
        assert not UserChannelBinding.objects.filter(user=member_user, channel_type=ChannelType.DISCORD).exists()


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
