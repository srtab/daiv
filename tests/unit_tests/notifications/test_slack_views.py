from __future__ import annotations

from unittest.mock import patch

from django.utils import timezone

import pytest
from notifications.choices import ChannelType
from notifications.models import UserChannelBinding


@pytest.mark.django_db
@pytest.mark.usefixtures("slack_channel_enabled")
class TestSlackConnect:
    URL = "/dashboard/notifications/channels/slack/"

    def test_requires_login(self, client):
        response = client.post(self.URL, {"username": "U0123ABCD"})
        assert response.status_code == 302
        assert "/accounts/login" in response["Location"] or "/login" in response["Location"]

    def test_creates_verified_binding_on_success(self, member_client, member_user):
        with patch("notifications.forms.verify_member_id", return_value=("U0123ABCD", None)):
            response = member_client.post(self.URL, {"username": "U0123ABCD"}, follow=False)
        assert response.status_code == 302
        binding = UserChannelBinding.objects.get(user=member_user, channel_type=ChannelType.SLACK)
        assert binding.address == "U0123ABCD"
        assert binding.is_verified is True
        assert binding.verified_at is not None

    def test_strips_surrounding_whitespace_from_member_id(self, member_client, member_user):
        with patch("notifications.forms.verify_member_id", return_value=("U0123ABCD", None)):
            member_client.post(self.URL, {"username": "  U0123ABCD  "})
        binding = UserChannelBinding.objects.get(user=member_user, channel_type=ChannelType.SLACK)
        assert binding.address == "U0123ABCD"

    def test_unknown_member_id_does_not_create_binding(self, member_client, member_user):
        with patch("notifications.forms.verify_member_id", return_value=(None, "Slack user not found.")):
            response = member_client.post(self.URL, {"username": "U0000NOPE"}, follow=True)
        assert response.status_code == 200
        msgs = [str(m) for m in list(response.context["messages"])]
        assert "Slack user not found." in msgs
        assert not UserChannelBinding.objects.filter(user=member_user, channel_type=ChannelType.SLACK).exists()

    def test_empty_member_id_does_not_create_binding(self, member_client, member_user):
        response = member_client.post(self.URL, {"username": " "}, follow=True)
        assert response.status_code == 200
        msgs = [str(m) for m in list(response.context["messages"])]
        assert "Member ID is required." in msgs
        assert not UserChannelBinding.objects.filter(user=member_user, channel_type=ChannelType.SLACK).exists()


@pytest.mark.django_db
def test_connect_returns_404_when_channel_disabled(member_client):
    response = member_client.post("/dashboard/notifications/channels/slack/", {"username": "U0123ABCD"})
    assert response.status_code == 404


@pytest.mark.django_db
class TestSlackDisconnect:
    URL = "/dashboard/notifications/channels/slack/delete/"

    def test_deletes_existing_binding(self, member_client, member_user):
        UserChannelBinding.objects.create(
            user=member_user,
            channel_type=ChannelType.SLACK,
            address="U0123ABCD",
            is_verified=True,
            verified_at=timezone.now(),
        )
        response = member_client.post(self.URL, follow=False)
        assert response.status_code == 302
        assert not UserChannelBinding.objects.filter(user=member_user, channel_type=ChannelType.SLACK).exists()

    def test_is_idempotent_when_no_binding(self, member_client):
        response = member_client.post(self.URL, follow=False)
        assert response.status_code == 302
