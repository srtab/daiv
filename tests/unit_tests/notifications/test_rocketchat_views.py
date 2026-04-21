from __future__ import annotations

from unittest.mock import patch

from django.utils import timezone

import pytest
from notifications.choices import ChannelType
from notifications.models import UserChannelBinding


@pytest.mark.django_db
class TestRocketChatConnect:
    URL = "/dashboard/notifications/channels/rocketchat/"

    def test_requires_login(self, client):
        response = client.post(self.URL, {"username": "alice"})
        assert response.status_code in (302, 401)

    def test_creates_verified_binding_on_success(self, member_client, member_user):
        with patch("notifications.views.verify_username", return_value=("u1", None)):
            response = member_client.post(self.URL, {"username": "alice"}, follow=False)
        assert response.status_code == 302
        binding = UserChannelBinding.objects.get(user=member_user, channel_type=ChannelType.ROCKETCHAT)
        assert binding.address == "alice"
        assert binding.is_verified is True
        assert binding.verified_at is not None

    def test_strips_leading_at_from_username(self, member_client, member_user):
        with patch("notifications.views.verify_username", return_value=("u1", None)):
            member_client.post(self.URL, {"username": "@alice"})
        binding = UserChannelBinding.objects.get(user=member_user, channel_type=ChannelType.ROCKETCHAT)
        assert binding.address == "alice"

    def test_invalid_username_does_not_create_binding(self, member_client, member_user):
        with patch("notifications.views.verify_username", return_value=(None, "User not found.")):
            response = member_client.post(self.URL, {"username": "nope"}, follow=True)
        assert response.status_code == 200
        msgs = [str(m) for m in list(response.context["messages"])]
        assert "User not found." in msgs
        assert not UserChannelBinding.objects.filter(user=member_user, channel_type=ChannelType.ROCKETCHAT).exists()

    def test_empty_username_does_not_create_binding(self, member_client, member_user):
        response = member_client.post(self.URL, {"username": ""}, follow=True)
        assert response.status_code == 200
        assert not UserChannelBinding.objects.filter(user=member_user, channel_type=ChannelType.ROCKETCHAT).exists()

    def test_updates_existing_binding_instead_of_duplicating(self, member_client, member_user):
        UserChannelBinding.objects.create(
            user=member_user, channel_type=ChannelType.ROCKETCHAT, address="old", is_verified=False
        )
        with patch("notifications.views.verify_username", return_value=("u1", None)):
            member_client.post(self.URL, {"username": "new"})
        bindings = UserChannelBinding.objects.filter(user=member_user, channel_type=ChannelType.ROCKETCHAT)
        assert bindings.count() == 1
        assert bindings.get().address == "new"
        assert bindings.get().is_verified is True


@pytest.mark.django_db
class TestRocketChatDisconnect:
    URL = "/dashboard/notifications/channels/rocketchat/delete/"

    def test_deletes_existing_binding(self, member_client, member_user):
        UserChannelBinding.objects.create(
            user=member_user,
            channel_type=ChannelType.ROCKETCHAT,
            address="alice",
            is_verified=True,
            verified_at=timezone.now(),
        )
        response = member_client.post(self.URL, follow=False)
        assert response.status_code == 302
        assert not UserChannelBinding.objects.filter(user=member_user, channel_type=ChannelType.ROCKETCHAT).exists()

    def test_is_idempotent_when_no_binding(self, member_client):
        response = member_client.post(self.URL, follow=False)
        assert response.status_code == 302
