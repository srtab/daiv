import pytest


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
