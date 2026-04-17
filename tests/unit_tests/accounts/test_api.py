from django.test import Client

import pytest

from accounts.models import Role, User


@pytest.mark.django_db
class TestUserSearchEndpoint:
    URL = "/api/accounts/users/search"

    def test_requires_authentication(self):
        client = Client()
        response = client.get(f"{self.URL}?q=ali")
        assert response.status_code == 401

    def test_returns_empty_for_short_query(self, member_client):
        response = member_client.get(f"{self.URL}?q=a")
        assert response.status_code == 200
        assert response.json() == []

    def test_matches_by_username(self, member_client):
        User.objects.create_user(username="alice", email="alice@t.com", password="x")  # noqa: S106
        response = member_client.get(f"{self.URL}?q=ali")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["username"] == "alice"

    def test_matches_by_email(self, member_client):
        User.objects.create_user(username="alice", email="bob@company.com", password="x")  # noqa: S106
        response = member_client.get(f"{self.URL}?q=bob@com")
        assert response.status_code == 200
        assert [u["username"] for u in response.json()] == ["alice"]

    def test_excludes_requesting_user(self, member_client, member_user):
        response = member_client.get(f"{self.URL}?q={member_user.username[:3]}")
        data = response.json()
        assert all(u["username"] != member_user.username for u in data)

    def test_honors_explicit_exclude_list(self, member_client):
        alice = User.objects.create_user(username="alice", email="a@t.com", password="x")  # noqa: S106
        User.objects.create_user(username="alicia", email="b@t.com", password="x")  # noqa: S106
        response = member_client.get(f"{self.URL}?q=ali&exclude={alice.pk}")
        data = response.json()
        assert all(u["id"] != alice.pk for u in data)

    def test_excludes_inactive_users(self, member_client):
        u = User.objects.create_user(username="alice", email="a@t.com", password="x")  # noqa: S106
        u.is_active = False
        u.save()
        response = member_client.get(f"{self.URL}?q=ali")
        assert response.json() == []

    def test_response_shape(self, member_client):
        User.objects.create_user(
            username="alice",
            email="a@t.com",
            password="x",  # noqa: S106
            name="Alice Doe",
            role=Role.MEMBER,
        )
        response = member_client.get(f"{self.URL}?q=ali")
        body = response.json()[0]
        assert set(body.keys()) == {"id", "username", "name", "email"}
        assert body["name"] == "Alice Doe"
        assert body["email"] == "a@t.com"
