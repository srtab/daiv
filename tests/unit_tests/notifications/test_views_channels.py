from django.utils import timezone

import pytest
from notifications.choices import ChannelType
from notifications.models import UserChannelBinding


@pytest.mark.django_db
class TestUserChannelsView:
    def test_requires_login(self, client):
        response = client.get("/accounts/channels/")
        assert response.status_code in (302, 401)

    def test_renders_email_row_for_authenticated_user(self, member_client, member_user):
        response = member_client.get("/accounts/channels/")
        assert response.status_code == 200
        content = response.content.decode()
        assert "Email" in content
        assert member_user.email in content


@pytest.mark.django_db
@pytest.mark.usefixtures("rocketchat_channel_enabled")
class TestUserChannelsRocketChatRow:
    URL = "/accounts/channels/"

    def test_renders_connect_form_when_no_binding(self, member_client):
        response = member_client.get(self.URL)
        content = response.content.decode()
        assert "Rocket Chat" in content
        assert 'name="username"' in content
        assert "Connect" in content

    def test_renders_disconnect_button_when_binding_exists(self, member_client, member_user):
        UserChannelBinding.objects.create(
            user=member_user,
            channel_type=ChannelType.ROCKETCHAT,
            address="alice",
            is_verified=True,
            verified_at=timezone.now(),
        )
        response = member_client.get(self.URL)
        content = response.content.decode()
        assert "Disconnect" in content
        assert "alice" in content


@pytest.mark.django_db
class TestUserChannelsRocketChatDisabled:
    URL = "/accounts/channels/"

    def test_row_hidden_when_rocketchat_disabled(self, member_client):
        response = member_client.get(self.URL)
        assert "Rocket Chat" not in response.content.decode()

    def test_connect_endpoint_returns_404_when_disabled(self, member_client):
        response = member_client.post("/dashboard/notifications/channels/rocketchat/", {"username": "alice"})
        assert response.status_code == 404
